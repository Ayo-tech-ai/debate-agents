import os
import sys
import time

import requests
import litellm

ROUNDS = int(os.getenv("DEBATE_ROUNDS", "5") or "5")
MODEL_FOR = "groq/openai/gpt-oss-120b"
MODEL_AGAINST = "groq/qwen/qwen3.8-27b"  # if this errors, change this line
PAUSE_SECONDS = 60
MAX_INTERJECTION = 500
LOG_FILE = "debate_log.txt"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "")
TOPIC = os.getenv("DEBATE_TOPIC", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

FOR_SYSTEM = (
    "You are debating FOR the motion. Argue your side firmly and clearly. "
    "Respond directly to the most recent point from the other side. "
    "Refer to the other side as 'the opposing view', never as 'you'. "
    "Keep each turn under 100 words. Do not repeat yourself."
)
AGAINST_SYSTEM = (
    "You are debating AGAINST the motion. Argue your side firmly and clearly. "
    "Respond directly to the most recent point from the other side. "
    "Refer to the other side as 'the opposing view', never as 'you'. "
    "Keep each turn under 100 words. Do not repeat yourself."
)


def send(text):
    """Post a line to the Telegram group and print it to the job log."""
    print(text + "\n")
    try:
        response = requests.post(
            f"{API}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text[:4000]},
            timeout=10,
        )
        if response.status_code != 200:
            print(f"Telegram send error {response.status_code}: {response.text[:200]}")
    except requests.RequestException as error:
        print(f"Telegram send failed (debate continues): {error}")
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
        print(f"Could not read Telegram this check: {error}")
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
    """Listens to the group for PAUSE_SECONDS and returns what you sent."""
    print(f"(Listening for your move in the group for {PAUSE_SECONDS}s.)")
    moves = []
    deadline = time.time() + PAUSE_SECONDS
    while time.time() < deadline:
        moves.extend(owner_messages(state))
        time.sleep(3)
    return moves


def ask(model, system, transcript):
    prompt = (
        "The debate so far:\n"
        + "\n".join(transcript)
        + "\n\nWrite your next point now."
    )
    response = litellm.completion(
        model=model,
        messages=[
            {"role": "system", "content": system},
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
    send(f"[Moderator] Debate: {TOPIC}")
    print(f"Rounds: {ROUNDS}. Steer from the Telegram group: "
          "a message interjects, 'stop' ends, silence continues.\n")

    try:
        for round_no in range(1, ROUNDS + 1):
            print(f"--- Round {round_no} of {ROUNDS} ---")

            point = ask(MODEL_FOR, FOR_SYSTEM, transcript)
            transcript.append(f"[For] {point}")
            send(f"[For] {point}")

            reply = ask(MODEL_AGAINST, AGAINST_SYSTEM, transcript)
            transcript.append(f"[Against] {reply}")
            send(f"[Against] {reply}")

            if round_no == ROUNDS:
                break

            moves = wait_for_moves(state)
            if any(m.lower() in ("stop", "/stop") for m in moves):
                break
            for move in moves:
                if move.lower() == "continue":
                    continue
                move = move[:MAX_INTERJECTION]
                transcript.append(f"[Moderator] {move}")
                send(f"[Moderator] {move}")

    except Exception as error:
        print(f"\nStopped because of an error: {error}")

    send("[Moderator] Debate ended.")
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write("\n".join(transcript) + "\n\n" + "=" * 40 + "\n\n")
    print(f"Transcript saved to {LOG_FILE}")


if __name__ == "__main__":
    main()
