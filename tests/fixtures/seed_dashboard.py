#!/usr/bin/env python3
import os
import sys
import time

# Ensure worktree root is on python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.db import create_job, finish_job, init_db


def seed_database(db_path: str):
    init_db(db_path)
    now = time.time()

    consumers = [
        ("bypass", "bypass", "127.0.0.1"),
        ("rondeau-int-wpp", "key", "192.168.1.10"),
        ("rondeau-int-admin", "key", "192.168.1.20"),
    ]

    agents_models = [
        ("agy", "gemini-3.8-flash"),
        ("agy", "gemini-4-argon"),
        ("claude", "claude-sonnet-5"),
    ]

    job_idx = 0
    # Seed various jobs
    for key_name, auth_mode, ip in consumers:
        for agent, model in agents_models:
            job_idx += 1
            # Completed job
            j1 = create_job(
                agent=agent,
                prompt=f"Seed prompt {job_idx} for {key_name}",
                model=model,
                client_ip=ip,
                auth_mode=auth_mode,
                api_key_name=key_name,
                db_path=db_path,
            )
            finish_job(
                j1["id"],
                status="completed",
                exit_code=0,
                stdout='{"result": "success"}',
                input_tokens=150 + job_idx * 10,
                output_tokens=80 + job_idx * 5,
                total_tokens=230 + job_idx * 15,
                cost_usd=0.0015 + (job_idx * 0.0002),
                finished_at=now - (job_idx * 300),
                db_path=db_path,
            )

        # Seed 1 failed job per consumer
        j_fail = create_job(
            agent="agy",
            prompt=f"Failed task for {key_name}",
            model="gemini-3.8-flash",
            client_ip=ip,
            auth_mode=auth_mode,
            api_key_name=key_name,
            db_path=db_path,
        )
        finish_job(
            j_fail["id"],
            status="failed",
            exit_code=1,
            error=f"Simulated execution failure for {key_name}: model overloaded",
            finished_at=now - (job_idx * 120),
            db_path=db_path,
        )

        # Seed 1 pending job per consumer
        create_job(
            agent="agy",
            prompt=f"Pending task for {key_name}",
            model="gemini-3.8-flash",
            client_ip=ip,
            auth_mode=auth_mode,
            api_key_name=key_name,
            wait=0,
            db_path=db_path,
        )

    print(f"Successfully seeded {db_path}")


if __name__ == "__main__":
    target_db = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DB_PATH", "./data/smoke_test.db")
    seed_database(target_db)
