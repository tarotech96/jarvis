# JARVIS

A local assistant for Taro's own work: it reads his notes and repositories,
his Gmail and Calendar, Slack and Jira, and answers out loud. Everything it
touches outside itself is **read-only** — there is no code in this project
that can send mail, post a message, or edit an issue.
---

## Run it

```bash
cd apps/api
python3 agent/main.py
```

Then open <http://127.0.0.1:8080>.

**Through Turbo** (runs the API and validates the UI together):

```bash
yarn install       # once
yarn dev
```

**In Docker:**

```bash
yarn docker        # docker compose up --build
# or detached:
docker compose up -d
```

---

## Run with Python

```bash
cd apps/api
python3 agent/main.py
```

Open <http://127.0.0.1:8080>.
