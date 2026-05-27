# Cloud BMS Digital Twin with Auth + Database

This version is ready for Render/Railway-style deployment.

## Features

- Flask-SocketIO real-time telemetry
- PostgreSQL via `DATABASE_URL`
- SQLite fallback locally
- SQLAlchemy models
- Secure password hashing with Werkzeug
- Session-based authentication
- Default admin creation on first boot
- ReportLab PDF export
- `/health` route for cloud health checks

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open:

```text
http://localhost:5001
```

Default login:

```text
admin / admin12345
```

## Required production environment variables

```text
SECRET_KEY=long-random-secret
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-strong-password
DATABASE_URL=postgresql://...
CORS_ALLOWED_ORIGINS=*
```

## Render deployment

1. Push this folder to GitHub.
2. Create a PostgreSQL database on Render or use `render.yaml`.
3. Create a Web Service.
4. Build command:

```bash
pip install -r requirements.txt
```

5. Start command:

```bash
gunicorn --worker-class eventlet -w 1 app:app --bind 0.0.0.0:$PORT
```

6. Set env vars: `DATABASE_URL`, `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`.

## Railway deployment

1. Push to GitHub.
2. Create a Railway project from GitHub.
3. Add a PostgreSQL service.
4. Make sure `DATABASE_URL` is available to the app service.
5. Railway will use `railway.json` start command.

## Vercel note

Vercel can run Flask as serverless WSGI, but this BMS app depends on a long-running background simulator and Socket.IO streaming. Use Render or Railway for the complete real-time app.
