# Whiskey Power FEFFL

A self-contained Streamlit app for a 14-team full-PPR fantasy football league.
It combines lineup optimization, waiver and FAAB tools, trade analysis, DST
and kicker streaming, keeper evaluation, and an AI-coach prompt.

## Run locally

Use Python 3.11, as pinned in `runtime.txt`.

```powershell
python -m pip install -r requirements.txt
streamlit run app.py
```

The app starts with sample league data. Use the sidebar to sync a CBS league
or upload a CBS/FantasyPros CSV. Live NFL usage and matchup analysis requires
the dependencies in `requirements.txt` and an internet connection.

## Deploy on Streamlit Community Cloud

1. Create an app pointing to `app.py` on the `main` branch.
2. Select Python 3.11. Streamlit installs Python dependencies from
   `requirements.txt` and system packages from `packages.txt`.
3. Add CBS login values as Streamlit secrets only if you use CBS auto-login.
   Never commit passwords, session cookies, or `.streamlit/secrets.toml`.

The app can also take a CBS session cookie directly in the sidebar. The
`Whiskey.jpg` logo is used to generate the app icon on first run.
