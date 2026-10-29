import os
import random
import sys
import time

import requests
import litellm

TURNS = int(os.getenv("DEBATE_ROUNDS", "5") or "5")  # total points; 5 means For/Against alternating 5 times
MODEL_FOR = "groq/openai/gpt-oss-120b"
MODEL_AGAINST = "groq/qwen/qwen3.8-27b"  # if this errors, change this line
PAUSE_EVERY = 2          # pause after every 2 points (one full exchange)
PAUSE_SECONDS = 60
CHECK_SECONDS = 3
MAX_INTERJECTION = 500
LOG_FILE = "debate_log.txt"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "")
TOPIC = os.getenv("DEBATE_TOPIC", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

SIDES = {
    "For": {
        "model": MODEL_FOR,
        "system": (
            "You are debating FOR the motion. Argue your side firmly and clearly. "
            "Refer to the other side as 'the opposing view', never as 'you'. "
            "Keep each turn under 100 words. Do not repeat yourself."
        ),
    },
    "Against": {
        "model": MODEL_AGAINST,
        "system": (
            "You are debating AGAINST the motion. Argue your side firmly and clearly. "
            "Refer to the other side as 'the opposing view', never as 'you'. "
            "Keep each turn under 100 words. Do not repeat yourself."
        ),
    },
}


def normalise(text):
    """Lowercase and strip, and drop '@botname' from commands like /stop@mybot."""
    text = text.strip().lower()
    if text.startswith("/"):
        text = text.split("@")[0]
    return text


def send(text):
    """Post a line to the Telegram group and print it to the job log."""
    print(text + "\n", flush=True)
    try:
        response = requests.post(
            f"{API}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text[:4000]},
            timeout=10,
        )
        if response.status_code != 200:
            print(f"Telegram send error {response.status_code}: {response.text[:200]}", flush=True)
    except requests.RequestException as error:
        print(f"Telegram send failed (debate continues): {error}", flush=True)
    time.sleep(3)  # keeps the group under Telegram's per-minute limit


def get_updates(offset=None):
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    response = requests.get(f"{API}/getUpdates", params=params, timeout=15)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {data}")
    return data["result"]


def skip_old_messages():
    """Ignore anything sent to the bot before this run started."""
    updates = get_updates()
    return updates[-1]["update_id"] + 1 if updates else None


def owner_messages(state):
    """New text messages from you in the group. Everyone else is ignored."""
    try:
        updates = get_updates(state["offset"])
    except (requests.RequestException, RuntimeError) as error:
        print(f"Could not read Telegram this check: {error}", flush=True)
        return []
    texts = []
    for update in updates:
        state["offset"] = update["update_id"] + 1
        message = update.get("message") or {}
        if str(message.get("chat", {}).get("id")) != CHAT_ID:
            continue
        if str(message.get("from", {}).get("id")) != OWNER_ID:
            continue
        text = (message.get("text") or "").strip()
        if text:
            texts.append(text)
    return texts


def wait_for_moves(state):
    """Returns (moves, action) where action is 'stop', 'continue' or 'timeout'."""
    print(f"(Pause: send /cont to go on, /stop to end, or a message to interject. "
          f"Continues automatically after {PAUSE_SECONDS}s.)", flush=True)
    moves = []
    deadline = time.time() + PAUSE_SECONDS
    while time.time() < deadline:
        for text in owner_messages(state):
            command = normalise(text)
            if command in ("/stop", "stop"):
                return moves, "stop"
            if command in ("/cont", "cont", "/continue", "continue"):
                return moves, "continue"
            moves.append(text)
        time.sleep(CHECK_SECONDS)
    return moves, "timeout"


def pick_starter():
    """Chosen once per topic. The sides then strictly alternate."""
    return random.choice(["For", "Against"])


def ask(side, transcript):
    """Asks the agent for one turn. The prompt depends on whether the other side has spoken yet."""
    other = "Against" if side == "For" else "For"
    other_has_spoken = any(line.startswith(f"[{other}]") for line in transcript)

    if other_has_spoken:
        instruction = (
            "Respond directly to the most recent point from the opposing view, "
            "then advance your own argument."
        )
    else:
        instruction = (
            "You are opening the debate. The opposing view has not spoken yet, "
            "so do NOT refer to it. Make your opening point on the topic."
        )

    prompt = (
        "The debate so far:\n"
        + "\n".join(transcript)
        + f"\n\n{instruction}\nWrite your point now."
    )
    response = litellm.completion(
        model=SIDES[side]["model"],
        messages=[
            {"role": "system", "content": SIDES[side]["system"]},
            {"role": "user", "content": prompt},
        ],
        max_tokens=400,
    )
    return response.choices[0].message.content.strip()


def main():
    if not (TOKEN and CHAT_ID and OWNER_ID):
        sys.exit("Missing a Telegram secret (token, chat ID or owner ID).")
    if not TOPIC:
        sys.exit("No topic given.")

    state = {"offset": skip_old_messages()}
    transcript = [f"[Moderator] Topic: {TOPIC}"]

    starter = pick_starter()
    order = [starter, "Against" if starter == "For" else "For"]

    send(f"[Moderator] Debate: {TOPIC}")
    send(f"[Moderator] {starter} opens. The sides then alternate until the end.")
    print(f"Points: {TURNS}. Commands in the group: /cont, /stop, or a message to interject.\n",
          flush=True)

    try:
        for turn in range(TURNS):
            side = order[turn % 2]  # strict alternation from the chosen starter
            point = ask(side, transcript)
            transcript.append(f"[{side}] {point}")
            send(f"[{side}] {point}")

            is_last = turn == TURNS - 1
            if is_last or (turn + 1) % PAUSE_EVERY != 0:
                continue

            moves, action = wait_for_moves(state)
            if action == "stop":
                send("[Moderator] Stopped by the moderator.")
                break
            for move in moves:
                move = move[:MAX_INTERJECTION]
                transcript.append(f"[Moderator] {move}")
                send(f"[Moderator] {move}")

    except Exception as error:
        print(f"\nStopped because of an error: {error}", flush=True)

    send("[Moderator] Debate ended.")
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write("\n".join(transcript) + "\n\n" + "=" * 40 + "\n\n")
    print(f"Transcript saved to {LOG_FILE}", flush=True)


if __name__ == "__main__":
    main()
