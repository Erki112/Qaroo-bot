import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://your-domain.com/webhook')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
