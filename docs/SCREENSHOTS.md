# Screenshots — how to add them

The README expects three images in this folder:

| File | What to capture |
|---|---|
| `screenshot-auth.png` | The sign-in screen (`/` logged out) |
| `screenshot-dashboard.png` | The dashboard with an uploaded document |
| `screenshot-results.png` | Extracted text + entities + confidence badge |

Steps:

1. Run the app: `python -X utf8 app.py`
2. Open http://127.0.0.1:5000 and take the screenshots (~1200px wide works best)
3. Save them in this `docs/` folder with the exact names above
4. Commit & push — the README picks them up automatically

Tip: redact or use a throwaway account — screenshots end up public on GitHub.
