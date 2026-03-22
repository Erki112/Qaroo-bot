import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import redis
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, WebAppInfo, FSInputFile
)
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit

# Config
from config import BOT_TOKEN, WEBHOOK_URL, REDIS_URL

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Redis client
rdb = redis.from_url(REDIS_URL)

# Game state
games: Dict[str, Dict] = {}
BINGO_NUMBERS = list(range(1, 76))  # Standard 75-ball bingo

class BingoGame:
    def __init__(self, host_id: int):
        self.host_id = host_id
        self.game_id = str(uuid.uuid4())
        self.players: List[Dict] = []
        self.grid: List[List[int]] = self._generate_grid()
        self.called_numbers: List[int] = []
        self.status = "waiting"  # waiting, active, finished
        self.winner = None
        self.start_time = datetime.now()
    
    def _generate_grid(self) -> List[List[int]]:
        """Generate 5x5 bingo grid with center FREE"""
        numbers = BINGO_NUMBERS.copy()
        random.shuffle(numbers)
        
        grid = []
        for i in range(5):
            row = []
            for j in range(5):
                if i == 2 and j == 2:  # Center FREE
                    row.append(0)
                else:
                    row.append(numbers.pop())
            grid.append(row)
        return grid
    
    def mark_number(self, number: int) -> bool:
        """Mark number if called. Returns True if BINGO detected"""
        if number in self.called_numbers:
            return False
        
        self.called_numbers.append(number)
        
        # Check for winner
        for player in self.players:
            if self._check_bingo(player['grid']):
                self.winner = player['user_id']
                self.status = "finished"
                return True
        return False
    
    def _check_bingo(self, grid: List[List[int]]) -> bool:
        """Check if grid has BINGO"""
        # Check rows
        for row in grid:
            if all(cell == 0 or cell in self.called_numbers for cell in row):
                return True
        
        # Check columns
        for col in range(5):
            if all(grid[row][col] == 0 or grid[row][col] in self.called_numbers 
                   for row in range(5)):
                return True
        
        # Check diagonals
        if all(grid[i][i] == 0 or grid[i][i] in self.called_numbers for i in range(5)):
            return True
        if all(grid[i][4-i] == 0 or grid[i][4-i] in self.called_numbers for i in range(5)):
            return True
        
        return False

# Telegram Bot Handlers
@router.message(CommandStart())
async def start_handler(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Play Bingo", web_app=WebAppInfo(url=f"{WEBHOOK_URL}/webapp"))]
    ])
    await message.answer(
        "🎉 Welcome to <b>Telegram Bingo Bot</b>!\n\n"
        "Click the button below to start playing with your 5x5 bingo card!",
        reply_markup=keyboard
    )

@router.callback_query(F.data.startswith("new_game:"))
async def new_game(callback: CallbackQuery):
    game_id = callback.data.split(":")[1]
    game = games.get(game_id)
    
    if not game:
        await callback.answer("Game not found!", show_alert=True)
        return
    
    game.status = "active"
    game.called_numbers = []
    game.winner = None
    
    # Notify all players
    for player in game.players:
        await bot.send_message(
            player['user_id'],
            f"🎯 <b>New Round Started!</b>\n"
            f"Game ID: <code>{game_id}</code>\n"
            f"Click 'Play Bingo' to join!"
        )
    
    # Emit to webapp
    socketio.emit('game_update', {
        'game_id': game_id,
        'status': game.status,
        'called_numbers': game.called_numbers,
        'winner': game.winner
    }, room=f"game_{game_id}")
    
    await callback.answer("New round started!")

@router.message(Command("call"))
async def call_number(message: Message):
    """Admin command to call a number"""
    try:
        number = int(message.text.split()[1])
        if 1 <= number <= 75:
            game_id = rdb.get(f"user_game:{message.from_user.id}")
            if game_id:
                game = games.get(game_id.decode())
                if game and game.status == "active":
                    if game.mark_number(number):
                        await message.answer(f"🎉 <b>BINGO!</b> Winner: {game.winner}")
                        socketio.emit('game_update', {
                            'game_id': game.game_id,
                            'status': 'finished',
                            'called_numbers': game.called_numbers,
                            'winner': game.winner
                        }, room=f"game_{game.game_id}")
                    else:
                        await message.answer(f"✅ Number <b>{number}</b> called!")
                        socketio.emit('number_called', {'number': number}, room=f"game_{game.game_id}")
        else:
            await message.answer("Please use a number between 1-75")
    except (IndexError, ValueError):
        await message.answer("Usage: /call 42")

# Flask Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/webapp')
def webapp():
    return render_template('index.html')

@app.route('/webhook', methods=['POST'])
async def webhook():
    update = request.get_json()
    # Process Telegram WebApp data
    if 'initData' in update:
        # Handle WebApp initialization
        pass
    return jsonify({'status': 'ok'})

@app.route('/api/game/<game_id>')
def get_game(game_id: str):
    game = games.get(game_id)
    if not game:
        return jsonify({'error': 'Game not found'}), 404
    
    return jsonify({
        'game_id': game.game_id,
        'host_id': game.host_id,
        'players': len(game.players),
        'status': game.status,
        'called_numbers': game.called_numbers,
        'winner': game.winner,
        'grid': game.grid
    })

@app.route('/api/game/create', methods=['POST'])
def create_game():
    data = request.json
    user_id = data.get('user_id')
    
    game = BingoGame(host_id=user_id)
    games[game.game_id] = game
    
    rdb.setex(f"user_game:{user_id}", 3600, game.game_id)
    
    return jsonify({
        'game_id': game.game_id,
        'grid': game.grid,
        'status': game.status
    })

@app.route('/api/game/<game_id>/join', methods=['POST'])
def join_game(game_id: str):
    data = request.json
    user_id = data.get('user_id')
    username = data.get('username', '')
    
    game = games.get(game_id)
    if not game:
        return jsonify({'error': 'Game not found'}), 404
    
    if any(p['user_id'] == user_id for p in game.players):
        return jsonify({'error': 'Already joined'})
    
    player_grid = game._generate_grid()
    game.players.append({
        'user_id': user_id,
        'username': username,
        'grid': player_grid
    })
    
    rdb.setex(f"user_game:{user_id}", 3600, game_id)
    
    socketio.emit('player_joined', {
        'username': username,
        'total_players': len(game.players)
    }, room=f"game_{game_id}")
    
    return jsonify({
        'success': True,
        'grid': player_grid,
        'players': len(game.players)
    })

@app.route('/api/game/<game_id>/call/<int:number>', methods=['POST'])
def call_number_api(game_id: str, number: int):
    game = games.get(game_id)
    if not game or game.status != "active":
        return jsonify({'error': 'Game not active'}), 400
    
    if game.mark_number(number):
        socketio.emit('game_won', {
            'winner': game.winner,
            'number': number
        }, room=f"game_{game_id}")
        return jsonify({'bingo': True, 'winner': game.winner})
    
    socketio.emit('number_called', {'number': number}, room=f"game_{game_id}")
    return jsonify({'success': True, 'bingo': False})

# SocketIO Events
@socketio.on('join_game')
def handle_join_game(data):
    game_id = data['game_id']
    join_room(f"game_{game_id}")
    emit('game_state', games[game_id].__dict__ if game_id in games else {})

@socketio.on('call_number')
def handle_call_number(data):
    game_id = data['game_id']
    number = data['number']
    game = games.get(game_id)
    
    if game and game.status == "active":
        emit('number_called', {'number': number}, room=f"game_{game_id}")

if __name__ == '__main__':
    # Start Flask with SocketIO
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
