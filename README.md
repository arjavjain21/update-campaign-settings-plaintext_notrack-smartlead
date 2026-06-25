# Smartlead Campaign Settings Updater

A simple internal Streamlit app for applying required Smartlead campaign settings in bulk.

## What it does

1. Shows a one-level password login.
2. Accepts campaign IDs using either CSV upload or comma-separated manual input.
3. Fetches Smartlead campaigns.
4. Finds the provided parent campaign IDs.
5. Finds subsequences where `parent_campaign_id` matches one of the provided IDs.
6. Shows an automatic preview of the update plan.
7. Applies these settings to selected parent campaigns and subsequences:
   - `track_settings`: `['DONT_TRACK_EMAIL_OPEN', 'DONT_TRACK_LINK_CLICK']`
   - `send_as_plain_text`: `true`
   - `enable_ai_esp_matching`: `false`
8. Verifies the settings after a live update.
9. Provides CSV and ZIP downloads.

## Local setup

```bash
cd smartlead_streamlit_app
pip install -r requirements.txt
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Edit `.streamlit/secrets.toml`:

```toml
APP_PASSWORD = "replace-this-with-a-strong-password"
SMARTLEAD_API_KEY = "replace-this-with-your-smartlead-api-key"

# Optional. Use only if you want the app to fetch campaigns for one client_id.
# SMARTLEAD_CLIENT_ID_FILTER = "123456"
```

Run:

```bash
streamlit run app.py
```

## Streamlit Community Cloud secrets

In Streamlit Cloud, add the same values in the app's Secrets section:

```toml
APP_PASSWORD = "replace-this-with-a-strong-password"
SMARTLEAD_API_KEY = "replace-this-with-your-smartlead-api-key"
```

Then restart the app after changing secrets.

## Environment variable alternative

Instead of Streamlit secrets, you can set:

```bash
export STREAMLIT_APP_PASSWORD="replace-this-with-a-strong-password"
export SMARTLEAD_API_KEY="replace-this-with-your-smartlead-api-key"
streamlit run app.py
```

## Operator flow

1. Log in.
2. Upload a CSV or paste campaign IDs.
3. Choose only the required options:
   - Dry run
   - Include parent campaigns
   - Include subsequences
4. Review the automatic preview.
5. Keep dry run on to download a preview CSV.
6. Turn dry run off only when you are ready to update Smartlead.

## Internal defaults

These are intentionally not shown to the operator:

- Concurrency: 8
- Request timeout: 60 seconds
- Max retries: 4
- Verification after live update: enabled
- Legacy tracking fallback on 422: enabled

Change these constants in `app.py` only if needed.
