import logging

from .bot import VerifyBot
from .config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("aiohttp.access").disabled = True
config = Config.load()
VerifyBot(config).run(config.discord_token, log_handler=None)
