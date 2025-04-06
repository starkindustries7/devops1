import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler
from telegram.ext.filters import Filters  # Updated import

# Replace 'YOUR_TOKEN' with the token from BotFather
TOKEN = '7209318843:AAGtRC7y_hN4SyTNyy7KVpCM1Atz2zgNxEA'

def start(update, context):
    update.message.reply_text("M🙊dda Koodu vachi....😮")

def echo(update, context):
    update.message.reply_text(update.message.text)

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, echo))
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()