"""
One-time YouTube login.

Run on a machine with a web browser:

    python -m app.youtube_auth

It opens Google's consent screen and saves ``token.pickle`` in the project
root. The Telegram bot reuses (and auto-refreshes) that token; copy it to
the server if the bot runs elsewhere.
"""

from app.services.youtube_service import authenticate


def main() -> None:
    authenticate(allow_browser=True)
    print("✅ YouTube connected — token.pickle saved. The bot can now publish videos.")


if __name__ == "__main__":
    main()
