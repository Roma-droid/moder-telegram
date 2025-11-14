from moder_telegram.app import run
import os

# Optionally set BOT_TOKEN and ADMINS here instead of using .env or external
# environment variables. Leave as None to read from the environment as usual.
# Examples:
# BOT_TOKEN = "123456:ABC-def"
# ADMINS = "6209247387,123456789"
BOT_TOKEN = ''
ADMINS = ''


if __name__ == "__main__":
	# If values provided in this file, write them into environment so the app
	# reads them (the app reads ADMINS from os.environ).
	if BOT_TOKEN:
		os.environ["BOT_TOKEN"] = BOT_TOKEN
	if ADMINS:
		os.environ["ADMINS"] = ADMINS

	run()
