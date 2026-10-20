"""One question from the terminal, asked as WORKFORCE_USER, against the readers the Slack app uses."""

import asyncio
import os
import sys

from workforce.analyst import answer
from workforce.slack_app import render
from workforce.sources import Readers

if __name__ == "__main__":
    print(
        render(
            asyncio.run(
                answer(
                    " ".join(sys.argv[1:]),
                    readers=Readers.from_env(user=os.environ.get("WORKFORCE_USER", "U_ENG")),
                )
            )
        )
    )
