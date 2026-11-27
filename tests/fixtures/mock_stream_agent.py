#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["agy", "claude"], default="agy")
    args = parser.parse_args()

    turn = 0
    conv_id = f"mock-conv-{os.getpid()}"

    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)

        turn += 1
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if args.mode == "agy":
            prompt = data.get("message", {}).get("content", "")
        else:
            content_list = data.get("message", {}).get("content", [])
            prompt = content_list[0].get("text", "") if content_list else ""

        if "CRASH" in prompt:
            sys.exit(1)
        if "TIMEOUT" in prompt:
            time.sleep(30)
        if "WAIT_GATE:" in prompt:
            # Format: WAIT_GATE:<gate_filepath>:<label> (preserve colons in gate_filepath via rsplit)
            first_line = prompt.splitlines()[0]
            gate_path = first_line.split("WAIT_GATE:", 1)[1].rsplit(":", 1)[0].strip()
            deadline = time.time() + 10.0
            while time.time() < deadline and not os.path.exists(gate_path):
                time.sleep(0.02)
            if not os.path.exists(gate_path):
                sys.stderr.write(f"WAIT_GATE timed out waiting for {gate_path}\n")
                sys.stderr.flush()
                sys.exit(1)
        if "SLOW_TURN:" in prompt:
            # Format: SLOW_TURN:<seconds>:<label>
            rest = prompt.split("SLOW_TURN:", 1)[1]
            parts = rest.split(":", 1)
            delay_s = float(parts[0])
            time.sleep(delay_s)
        if "NON_DICT_JSON" in prompt:
            sys.stdout.write("[1, 2, 3]\n")
            sys.stdout.flush()
            continue
        if "SOFT_ERROR" in prompt:
            long_err = ("Not logged in · Please run /login — " + ("Q" * 500)) if "LONG_AUTH" in prompt else "Transient tool execution failure"
            if args.mode == "agy":
                err_payload = {
                    "conversation_id": conv_id,
                    "status": "ERROR",
                    "response": "",
                    "error": long_err,
                    "usage": {
                        "input_tokens": 100 * turn,
                        "output_tokens": 50 * turn,
                        "cache_read_tokens": 0,
                        "total_tokens": 150 * turn,
                    },
                }
                sys.stdout.write(json.dumps({"event": "result", "result": err_payload}) + "\n")
                sys.stdout.flush()
                continue
            else:
                err_evt = {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "session_id": conv_id,
                    "result": long_err,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
                    "total_cost_usd": round(0.0012 * turn, 6),
                }
                sys.stdout.write(json.dumps(err_evt) + "\n")
                sys.stdout.flush()
                continue
        if "AUTH_EXPIRED" in prompt:
            if "SPLIT_LONG" in prompt:
                sys.stderr.write("X" * 400 + "\nAuthenti")
                sys.stderr.flush()
                time.sleep(0.05)
                sys.stderr.write("cation required: " + ("Y" * 500) + "\n")
                sys.stderr.flush()
                time.sleep(60)
                sys.exit(1)
            if args.mode == "agy":
                sys.stderr.write(
                    "Authentication required. Please visit the URL to log in:\n"
                    "Waiting for authentication (timeout 60s)...\n"
                )
                sys.stderr.flush()
                time.sleep(60)
                sys.exit(1)
            else:
                err_evt = {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "session_id": conv_id,
                    "result": "Not logged in · Please run /login",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0},
                    "total_cost_usd": 0,
                }
                sys.stdout.write(json.dumps(err_evt) + "\n")
                sys.stdout.flush()
                sys.exit(1)
        if "CHATTY_STDERR" in prompt:
            sys.stderr.write("E" * (130 * 1024) + "\n")
            sys.stderr.flush()

        resp_text = f"Echo [turn {turn}]: {prompt}"
        if "LARGE_OUTPUT" in prompt:
            resp_text = "L" * (256 * 1024)
        elif "OCR_429" in prompt:
            resp_text = "The receipt says $ 429,00 and store address is AV ITALIA 4429"
        elif "UNREADABLE" in prompt:
            resp_text = "Error: attachment file missing and could not be found or read from disk"
        elif "CHECK_ATTACHMENT:" in prompt:
            att_name = prompt.split("CHECK_ATTACHMENT:", 1)[1].split()[0].strip()
            att_path = os.path.join(os.getcwd(), "attachments", att_name)
            if os.path.exists(att_path):
                resp_text = f"ATTACHMENT_FOUND: {att_name} ({os.path.getsize(att_path)} bytes)"
            else:
                resp_text = f"ATTACHMENT_NOT_FOUND: {att_name}"

        if args.mode == "agy":
            if turn == 1:
                sys.stdout.write(json.dumps({"event": "init", "conversation_id": conv_id}) + "\n")
            sys.stdout.write(json.dumps({"event": "step_update", "step": turn}) + "\n")
            result_payload = {
                "conversation_id": conv_id,
                "status": "SUCCESS",
                "response": resp_text,
                "duration_seconds": 0.5,
                "num_turns": turn,
                "usage": {
                    "input_tokens": 100 * turn,
                    "output_tokens": 50 * turn,
                    "cache_read_tokens": 20 * turn,
                    "total_tokens": 170 * turn,
                },
            }
            sys.stdout.write(json.dumps({"event": "result", "result": result_payload}) + "\n")
            sys.stdout.flush()
        else:
            if turn == 1:
                sys.stdout.write(json.dumps({"type": "system", "session_id": conv_id}) + "\n")
            result_evt = {
                "type": "result",
                "subtype": "success",
                "session_id": conv_id,
                "result": resp_text,
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 15,
                },
                "total_cost_usd": round(0.0012 * turn, 6),
            }
            sys.stdout.write(json.dumps(result_evt) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
