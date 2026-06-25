# Smartlead Campaign Settings Updater

This Streamlit app converts the notebook flow into a simple internal web app.

## What it does

1. Shows a one-level password login.
2. Accepts campaign IDs using either:
   - CSV upload
   - Comma-separated manual input
3. Fetches all Smartlead campaigns.
4. Finds the provided parent campaign IDs.
5. Finds subsequences where `parent_campaign_id` matches one of the provided IDs.
6. Builds an update plan.
7. Applies these settings to the selected parent campaigns and subsequences:
   - `track_settings`: `["DONT_TRACK_EMAIL_OPEN", "DONT_TRACK_LINK_CLICK"]`
   - `send_as_plain_text`: `true`
   - `enable_ai_esp_matching`: `false`
8. Optionally verifies the settings after the update.
9. Provides a final CSV download and a ZIP with all output CSVs.

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
```

Run:

```bash
streamlit run app.py
```

## Environment variable alternative

Instead of Streamlit secrets, you can set:

```bash
export STREAMLIT_APP_PASSWORD="replace-this-with-a-strong-password"
streamlit run app.py
```

## Operational recommendation

Start with dry run enabled. Review the update plan. Then run live.

If Smartlead starts returning many `429` responses, lower concurrency.
