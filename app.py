import concurrent.futures
import copy
import hmac
import io
import json
import math
import os
import random
import re
import time
import zipfile
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st


BASE_URL = "https://server.smartlead.ai/api/v1"

# Operator-facing UI stays intentionally simple. These are safe internal defaults.
DEFAULT_CONCURRENCY = 8
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 4
DEFAULT_RETRY_BASE_SECONDS = 1.5
DEFAULT_RETRY_MAX_SECONDS = 30.0
DEFAULT_VERIFY_AFTER_LIVE_UPDATE = True
DEFAULT_ENABLE_LEGACY_TRACKING_FALLBACK_ON_422 = True
DEFAULT_INCLUDE_EXISTING_CAMPAIGN_NAME_IN_PAYLOAD = False
DEFAULT_SKIP_ARCHIVED_CAMPAIGNS = False

PRIMARY_SETTINGS_PAYLOAD = {
    "track_settings": ["DONT_TRACK_EMAIL_OPEN", "DONT_TRACK_LINK_CLICK"],
    "send_as_plain_text": True,
    "enable_ai_esp_matching": False,
}

LEGACY_TRACKING_PAYLOAD = copy.deepcopy(PRIMARY_SETTINGS_PAYLOAD)
LEGACY_TRACKING_PAYLOAD["track_settings"] = ["DONT_EMAIL_OPEN", "DONT_LINK_CLICK"]

CAMPAIGN_ID_COLUMN_CANDIDATES = [
    "campaign_id",
    "campaign id",
    "Campaign ID",
    "id",
    "campaign",
    "smartlead_campaign_id",
    "smartlead campaign id",
]


def clean_col_name(col: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")


def normalize_dataframe_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, str]]:
    df = df.copy()
    original_cols = list(df.columns)
    normalized_cols = [clean_col_name(c) for c in original_cols]
    df.columns = normalized_cols
    mapping = dict(zip(normalized_cols, original_cols))
    return df, mapping


def first_existing_column(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    normalized_candidates = [clean_col_name(c) for c in candidates]
    for candidate in normalized_candidates:
        if candidate in df.columns:
            return candidate
    return None


def parse_int_id(value: Any) -> Optional[int]:
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    value_str = str(value).strip()
    if not value_str:
        return None

    try:
        num = float(value_str)
        if math.isfinite(num) and num.is_integer():
            return int(num)
    except Exception:
        pass

    match = re.search(r"\d+", value_str)
    if match:
        return int(match.group(0))

    return None


def parse_manual_campaign_ids(raw_text: str) -> Tuple[List[int], List[str]]:
    raw_parts = re.split(r"[\s,]+", raw_text.strip())
    parsed_ids: List[int] = []
    invalid_values: List[str] = []

    for raw_value in raw_parts:
        raw_value = raw_value.strip()
        if not raw_value:
            continue

        parsed_id = parse_int_id(raw_value)
        if parsed_id is None:
            invalid_values.append(raw_value)
        else:
            parsed_ids.append(parsed_id)

    return sorted(set(parsed_ids)), invalid_values


def read_campaign_ids_from_csv(uploaded_file) -> Tuple[List[int], pd.DataFrame, pd.DataFrame, str]:
    try:
        raw_df = pd.read_csv(uploaded_file)
    except UnicodeDecodeError:
        uploaded_file.seek(0)
        raw_df = pd.read_csv(uploaded_file, encoding="latin-1")

    normalized_df, original_column_mapping = normalize_dataframe_columns(raw_df)
    campaign_id_col = first_existing_column(normalized_df, CAMPAIGN_ID_COLUMN_CANDIDATES)

    if not campaign_id_col:
        accepted = ", ".join(CAMPAIGN_ID_COLUMN_CANDIDATES[:5])
        raise ValueError(f"Could not find a campaign ID column. Accepted examples: {accepted}.")

    normalized_df["parsed_campaign_id"] = normalized_df[campaign_id_col].apply(parse_int_id)

    invalid_rows_df = normalized_df[normalized_df["parsed_campaign_id"].isna()].copy()
    valid_campaign_ids = (
        normalized_df["parsed_campaign_id"]
        .dropna()
        .astype(int)
        .drop_duplicates()
        .sort_values()
        .tolist()
    )

    return valid_campaign_ids, raw_df, invalid_rows_df, original_column_mapping.get(campaign_id_col, campaign_id_col)


def safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return {"raw_text": text}


def response_is_success(status_code: int, body: Any) -> bool:
    if status_code < 200 or status_code >= 300:
        return False

    if isinstance(body, dict):
        if body.get("success") is False:
            return False
        if body.get("ok") is False:
            return False

    return True


def flatten_for_csv(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def dataframe_flatten_for_csv(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten nested values for CSV export without using deprecated pandas APIs."""
    if df.empty:
        return df.copy()
    return df.apply(lambda series: series.map(flatten_for_csv))


def extract_campaign_list(api_response: Any) -> List[Dict[str, Any]]:
    if isinstance(api_response, list):
        return api_response

    if isinstance(api_response, dict):
        for key in ["campaigns", "data", "results", "items"]:
            value = api_response.get(key)
            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                for nested_key in ["campaigns", "data", "results", "items"]:
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, list):
                        return nested_value

    keys = list(api_response.keys()) if isinstance(api_response, dict) else "not a dict"
    raise ValueError(
        "Could not extract campaigns from Smartlead response. "
        f"Top-level type: {type(api_response).__name__}. Top-level keys: {keys}"
    )


def compact_campaign_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    preferred = [
        "id",
        "name",
        "status",
        "parent_campaign_id",
        "client_id",
        "track_settings",
        "send_as_plain_text",
        "enable_ai_esp_matching",
        "updated_at",
        "created_at",
    ]
    existing = [c for c in preferred if c in df.columns]
    remaining = [c for c in df.columns if c not in existing]
    return df[existing + remaining]


@st.cache_data(show_spinner=False, ttl=300)
def fetch_all_campaigns_cached(
    api_key: str,
    client_id_filter: Optional[int],
    request_timeout_seconds: int,
) -> List[Dict[str, Any]]:
    return fetch_all_campaigns(api_key, client_id_filter, request_timeout_seconds)


def fetch_all_campaigns(
    api_key: str,
    client_id_filter: Optional[int],
    request_timeout_seconds: int,
) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/campaigns/"
    params: Dict[str, Any] = {
        "api_key": api_key,
        "include_tags": "true",
    }

    if client_id_filter is not None:
        params["client_id"] = client_id_filter

    response = requests.get(url, params=params, timeout=request_timeout_seconds)
    response_text = response.text

    if response.status_code >= 400:
        raise RuntimeError(
            f"Smartlead returned HTTP {response.status_code}: {response_text[:3000]}"
        )

    api_response = response.json()
    return extract_campaign_list(api_response)


def prepare_campaigns_dataframe(campaigns: List[Dict[str, Any]]) -> pd.DataFrame:
    campaigns_df = pd.json_normalize(campaigns)

    if "id" not in campaigns_df.columns:
        raise ValueError("Smartlead campaigns response did not contain an 'id' field.")

    if "parent_campaign_id" not in campaigns_df.columns:
        campaigns_df["parent_campaign_id"] = None

    campaigns_df["id"] = pd.to_numeric(campaigns_df["id"], errors="coerce").astype("Int64")
    campaigns_df["parent_campaign_id"] = pd.to_numeric(
        campaigns_df["parent_campaign_id"],
        errors="coerce",
    ).astype("Int64")

    if "name" not in campaigns_df.columns:
        campaigns_df["name"] = ""

    if "status" not in campaigns_df.columns:
        campaigns_df["status"] = ""

    return campaigns_df


def build_update_plan(
    all_campaigns_df: pd.DataFrame,
    valid_campaign_ids: List[int],
    include_parent_campaigns: bool,
    include_subsequences: bool,
    skip_archived_campaigns: bool,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    input_campaign_ids_set = set(valid_campaign_ids)
    all_ids_set = set(all_campaigns_df["id"].dropna().astype(int).tolist())

    input_campaigns_found_df = all_campaigns_df[
        all_campaigns_df["id"].astype("Int64").isin(valid_campaign_ids)
    ].copy()

    missing_campaign_ids = sorted(list(input_campaign_ids_set - all_ids_set))
    missing_campaign_ids_df = pd.DataFrame({"campaign_id": missing_campaign_ids})

    subsequence_df = all_campaigns_df[
        all_campaigns_df["parent_campaign_id"].astype("Int64").isin(valid_campaign_ids)
    ].copy()

    candidate_ids = set()

    if include_parent_campaigns:
        candidate_ids.update(input_campaigns_found_df["id"].dropna().astype(int).tolist())

    if include_subsequences:
        candidate_ids.update(subsequence_df["id"].dropna().astype(int).tolist())

    if not candidate_ids:
        plan_df = pd.DataFrame()
        return plan_df, input_campaigns_found_df, subsequence_df, missing_campaign_ids_df

    plan_df = all_campaigns_df[
        all_campaigns_df["id"].astype("Int64").isin(sorted(candidate_ids))
    ].copy()

    plan_df["campaign_id"] = plan_df["id"].astype(int)
    plan_df["is_input_campaign"] = plan_df["campaign_id"].isin(input_campaign_ids_set)
    plan_df["is_subsequence_of_input"] = plan_df["parent_campaign_id"].astype("Int64").isin(valid_campaign_ids)
    plan_df["target_track_settings"] = json.dumps(PRIMARY_SETTINGS_PAYLOAD["track_settings"])
    plan_df["target_send_as_plain_text"] = PRIMARY_SETTINGS_PAYLOAD["send_as_plain_text"]
    plan_df["target_enable_ai_esp_matching"] = PRIMARY_SETTINGS_PAYLOAD["enable_ai_esp_matching"]

    if skip_archived_campaigns and "status" in plan_df.columns:
        plan_df = plan_df[plan_df["status"].astype(str).str.upper().ne("ARCHIVED")].copy()

    plan_df["sort_parent"] = plan_df["parent_campaign_id"].fillna(plan_df["campaign_id"]).astype(int)
    plan_df["sort_is_child"] = plan_df["parent_campaign_id"].notna().astype(int)
    plan_df = plan_df.sort_values(["sort_parent", "sort_is_child", "campaign_id"]).reset_index(drop=True)

    return plan_df, input_campaigns_found_df, subsequence_df, missing_campaign_ids_df


def select_plan_export_columns(plan_df: pd.DataFrame) -> pd.DataFrame:
    preferred_cols = [
        "campaign_id",
        "name",
        "status",
        "parent_campaign_id",
        "is_input_campaign",
        "is_subsequence_of_input",
        "track_settings",
        "send_as_plain_text",
        "enable_ai_esp_matching",
        "target_track_settings",
        "target_send_as_plain_text",
        "target_enable_ai_esp_matching",
    ]
    cols = [c for c in preferred_cols if c in plan_df.columns]
    return plan_df[cols].copy()


def post_settings_once(
    api_key: str,
    campaign_row: Dict[str, Any],
    payload: Dict[str, Any],
    payload_version: str,
    request_timeout_seconds: int,
    max_retries: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
    include_existing_campaign_name_in_payload: bool,
) -> Dict[str, Any]:
    campaign_id = int(campaign_row["campaign_id"])
    url = f"{BASE_URL}/campaigns/{campaign_id}/settings"
    request_payload = copy.deepcopy(payload)

    if include_existing_campaign_name_in_payload:
        campaign_name = campaign_row.get("name")
        if campaign_name is not None and str(campaign_name).strip():
            request_payload["name"] = str(campaign_name)

    params = {"api_key": api_key}

    for attempt in range(1, max_retries + 1):
        started_at = time.time()
        status_code = None
        body = None

        try:
            response = requests.post(
                url,
                params=params,
                json=request_payload,
                timeout=request_timeout_seconds,
            )
            status_code = response.status_code
            response_text = response.text
            body = safe_json_loads(response_text)
            elapsed_seconds = round(time.time() - started_at, 3)
            success = response_is_success(status_code, body)

            result = {
                "campaign_id": campaign_id,
                "campaign_name": campaign_row.get("name"),
                "parent_campaign_id": campaign_row.get("parent_campaign_id"),
                "is_input_campaign": campaign_row.get("is_input_campaign"),
                "is_subsequence_of_input": campaign_row.get("is_subsequence_of_input"),
                "payload_version": payload_version,
                "attempt": attempt,
                "status_code": status_code,
                "success": success,
                "elapsed_seconds": elapsed_seconds,
                "request_payload": request_payload,
                "response_body": body,
                "error": None,
            }

            if success:
                return result

            retryable_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
            if status_code in retryable_statuses and attempt < max_retries:
                retry_after = None
                if isinstance(body, dict):
                    retry_after = body.get("retry_after") or body.get("retryAfter")

                if retry_after is not None:
                    sleep_seconds = float(retry_after)
                else:
                    sleep_seconds = min(
                        retry_max_seconds,
                        retry_base_seconds * (2 ** (attempt - 1)) + random.uniform(0, 1.5),
                    )

                time.sleep(sleep_seconds)
                continue

            return result

        except Exception as exc:
            elapsed_seconds = round(time.time() - started_at, 3)
            error_text = repr(exc)

            if attempt < max_retries:
                sleep_seconds = min(
                    retry_max_seconds,
                    retry_base_seconds * (2 ** (attempt - 1)) + random.uniform(0, 1.5),
                )
                time.sleep(sleep_seconds)
                continue

            return {
                "campaign_id": campaign_id,
                "campaign_name": campaign_row.get("name"),
                "parent_campaign_id": campaign_row.get("parent_campaign_id"),
                "is_input_campaign": campaign_row.get("is_input_campaign"),
                "is_subsequence_of_input": campaign_row.get("is_subsequence_of_input"),
                "payload_version": payload_version,
                "attempt": attempt,
                "status_code": status_code,
                "success": False,
                "elapsed_seconds": elapsed_seconds,
                "request_payload": request_payload,
                "response_body": body,
                "error": error_text,
            }

    return {
        "campaign_id": campaign_id,
        "campaign_name": campaign_row.get("name"),
        "parent_campaign_id": campaign_row.get("parent_campaign_id"),
        "is_input_campaign": campaign_row.get("is_input_campaign"),
        "is_subsequence_of_input": campaign_row.get("is_subsequence_of_input"),
        "payload_version": payload_version,
        "attempt": max_retries,
        "status_code": status_code,
        "success": False,
        "elapsed_seconds": None,
        "request_payload": request_payload,
        "response_body": body,
        "error": "Exhausted retries without a result.",
    }


def update_one_campaign(
    api_key: str,
    campaign_row: Dict[str, Any],
    request_timeout_seconds: int,
    max_retries: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
    enable_legacy_tracking_fallback_on_422: bool,
    include_existing_campaign_name_in_payload: bool,
) -> Dict[str, Any]:
    primary_result = post_settings_once(
        api_key=api_key,
        campaign_row=campaign_row,
        payload=PRIMARY_SETTINGS_PAYLOAD,
        payload_version="primary",
        request_timeout_seconds=request_timeout_seconds,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        include_existing_campaign_name_in_payload=include_existing_campaign_name_in_payload,
    )

    if (
        enable_legacy_tracking_fallback_on_422
        and not primary_result.get("success")
        and primary_result.get("status_code") == 422
    ):
        legacy_result = post_settings_once(
            api_key=api_key,
            campaign_row=campaign_row,
            payload=LEGACY_TRACKING_PAYLOAD,
            payload_version="legacy_tracking_fallback",
            request_timeout_seconds=request_timeout_seconds,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            include_existing_campaign_name_in_payload=include_existing_campaign_name_in_payload,
        )
        legacy_result["primary_422_response_body"] = primary_result.get("response_body")
        return legacy_result

    return primary_result


def run_updates_with_progress(
    api_key: str,
    plan_df: pd.DataFrame,
    concurrency: int,
    request_timeout_seconds: int,
    max_retries: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
    enable_legacy_tracking_fallback_on_422: bool,
    include_existing_campaign_name_in_payload: bool,
) -> pd.DataFrame:
    rows = plan_df.to_dict("records")
    total = len(rows)
    results: List[Dict[str, Any]] = []

    progress_bar = st.progress(0)
    progress_text = st.empty()

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                update_one_campaign,
                api_key,
                row,
                request_timeout_seconds,
                max_retries,
                retry_base_seconds,
                retry_max_seconds,
                enable_legacy_tracking_fallback_on_422,
                include_existing_campaign_name_in_payload,
            )
            for row in rows
        ]

        for completed_count, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            results.append(result)

            success_count = sum(1 for r in results if r.get("success"))
            failure_count = completed_count - success_count
            progress_bar.progress(completed_count / total)
            progress_text.write(
                f"Progress: {completed_count:,}/{total:,} done. "
                f"Successful: {success_count:,}. Failed: {failure_count:,}."
            )

    return pd.DataFrame(results)


def normalize_track_settings_for_check(value: Any) -> set:
    if isinstance(value, list):
        return set(str(x) for x in value)

    try:
        if pd.isna(value):
            return set()
    except Exception:
        pass

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return set(str(x) for x in parsed)
        except Exception:
            pass
        return {value}

    return set()


def verify_updated_settings(
    api_key: str,
    plan_df: pd.DataFrame,
    client_id_filter: Optional[int],
    request_timeout_seconds: int,
) -> pd.DataFrame:
    refreshed_campaigns = fetch_all_campaigns(
        api_key=api_key,
        client_id_filter=client_id_filter,
        request_timeout_seconds=request_timeout_seconds,
    )
    refreshed_df = prepare_campaigns_dataframe(refreshed_campaigns)

    verify_df = refreshed_df[
        refreshed_df["id"].astype("Int64").isin(plan_df["campaign_id"].astype(int).tolist())
    ].copy()

    verify_df["campaign_id"] = verify_df["id"].astype(int)

    accepted_track_sets = [
        set(PRIMARY_SETTINGS_PAYLOAD["track_settings"]),
        set(LEGACY_TRACKING_PAYLOAD["track_settings"]),
    ]

    if "track_settings" in verify_df.columns:
        verify_df["track_settings_ok"] = verify_df["track_settings"].apply(
            lambda x: normalize_track_settings_for_check(x) in accepted_track_sets
        )
    else:
        verify_df["track_settings_ok"] = False

    if "send_as_plain_text" in verify_df.columns:
        verify_df["send_as_plain_text_ok"] = verify_df["send_as_plain_text"].astype(str).str.lower().isin(["true", "1"])
    else:
        verify_df["send_as_plain_text_ok"] = False

    if "enable_ai_esp_matching" in verify_df.columns:
        verify_df["enable_ai_esp_matching_ok"] = verify_df["enable_ai_esp_matching"].astype(str).str.lower().isin(["false", "0"])
    else:
        verify_df["enable_ai_esp_matching_ok"] = False

    verify_df["all_requested_settings_ok"] = (
        verify_df["track_settings_ok"]
        & verify_df["send_as_plain_text_ok"]
        & verify_df["enable_ai_esp_matching_ok"]
    )

    preferred_cols = [
        "campaign_id",
        "name",
        "status",
        "parent_campaign_id",
        "track_settings",
        "send_as_plain_text",
        "enable_ai_esp_matching",
        "track_settings_ok",
        "send_as_plain_text_ok",
        "enable_ai_esp_matching_ok",
        "all_requested_settings_ok",
        "updated_at",
    ]
    cols = [c for c in preferred_cols if c in verify_df.columns]
    return verify_df[cols].copy()


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    output = io.StringIO()
    df.to_csv(output, index=False)
    return output.getvalue().encode("utf-8-sig")


def build_zip_bytes(files: Dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zipf:
        for filename, df in files.items():
            zipf.writestr(filename, dataframe_to_csv_bytes(df))
    buffer.seek(0)
    return buffer.getvalue()


def get_secret_or_env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value:
        return str(value)

    try:
        value = st.secrets.get(name)
        if value:
            return str(value)
    except Exception:
        pass

    return None


def get_configured_password() -> Optional[str]:
    return get_secret_or_env("STREAMLIT_APP_PASSWORD") or get_secret_or_env("APP_PASSWORD")


def get_configured_api_key() -> Optional[str]:
    return get_secret_or_env("SMARTLEAD_API_KEY")


def get_optional_int_config(name: str) -> Optional[int]:
    raw_value = get_secret_or_env(name)
    if raw_value is None or not str(raw_value).strip():
        return None

    try:
        return int(str(raw_value).strip())
    except ValueError:
        return None


def require_password_login() -> None:
    configured_password = get_configured_password()

    if not configured_password:
        st.error("Password is not configured. Set APP_PASSWORD in .streamlit/secrets.toml or STREAMLIT_APP_PASSWORD as an environment variable.")
        st.stop()

    if st.session_state.get("authenticated"):
        return

    st.title("Smartlead Campaign Settings Updater")
    st.subheader("Login")

    submitted_password = st.text_input("Password", type="password")
    login_clicked = st.button("Login")

    if login_clicked:
        if hmac.compare_digest(submitted_password, configured_password):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.stop()


def render_summary_cards(
    valid_campaign_ids: List[int],
    input_found_count: int,
    subsequence_count: int,
    missing_count: int,
    plan_count: int,
) -> None:
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Input IDs", f"{len(valid_campaign_ids):,}")
    col2.metric("Found parents", f"{input_found_count:,}")
    col3.metric("Subsequences", f"{subsequence_count:,}")
    col4.metric("Missing IDs", f"{missing_count:,}")
    col5.metric("Plan rows", f"{plan_count:,}")


def main() -> None:
    st.set_page_config(
        page_title="Smartlead Campaign Settings Updater",
        page_icon="⚙️",
        layout="wide",
    )

    require_password_login()

    st.title("Smartlead Campaign Settings Updater")
    st.caption("Upload or paste parent campaign IDs, preview matched parents and subsequences, then apply the required Smartlead settings.")

    api_key = get_configured_api_key()
    if api_key:
        st.success("Smartlead API key loaded from Streamlit secrets.")
    else:
        api_key = st.text_input("Smartlead API key", type="password")
        if not api_key:
            st.warning("Add SMARTLEAD_API_KEY to Streamlit secrets to avoid asking operators for the API key every time.")

    client_id_filter = get_optional_int_config("SMARTLEAD_CLIENT_ID_FILTER")

    st.subheader("1. Campaign input")
    input_method = st.radio(
        "Choose input method",
        ["CSV upload", "Comma-separated campaign IDs"],
        horizontal=True,
    )

    valid_campaign_ids: List[int] = []
    invalid_manual_values: List[str] = []
    invalid_csv_rows_df = pd.DataFrame()
    detected_campaign_id_column = ""

    if input_method == "CSV upload":
        uploaded_file = st.file_uploader("Upload CSV containing campaign IDs", type=["csv"])
        if uploaded_file:
            try:
                valid_campaign_ids, _, invalid_csv_rows_df, detected_campaign_id_column = read_campaign_ids_from_csv(uploaded_file)
                st.success(f"Detected campaign ID column: {detected_campaign_id_column}")
            except Exception as exc:
                st.error(str(exc))
                st.stop()
    else:
        manual_ids_text = st.text_area(
            "Paste campaign IDs",
            placeholder="3529654, 3529659, 3529665",
            height=100,
        )
        valid_campaign_ids, invalid_manual_values = parse_manual_campaign_ids(manual_ids_text)

    if valid_campaign_ids:
        st.write(f"Parsed **{len(valid_campaign_ids):,} unique campaign IDs**.")
        st.dataframe(pd.DataFrame({"campaign_id": valid_campaign_ids}).head(1000), use_container_width=True)
    else:
        st.info("Add campaign IDs to preview the update plan.")

    if invalid_manual_values:
        st.warning(f"Skipped invalid manual values: {', '.join(invalid_manual_values[:50])}")

    if not invalid_csv_rows_df.empty:
        with st.expander(f"Invalid CSV rows skipped: {len(invalid_csv_rows_df):,}"):
            st.dataframe(invalid_csv_rows_df.head(1000), use_container_width=True)

    st.subheader("2. Run options")
    option_col1, option_col2, option_col3 = st.columns(3)
    with option_col1:
        dry_run = st.toggle("Dry run", value=True, help="Preview and export the plan without updating Smartlead.")
    with option_col2:
        include_parent_campaigns = st.toggle("Include parent campaigns", value=True)
    with option_col3:
        include_subsequences = st.toggle("Include subsequences", value=True)

    st.subheader("3. Settings to update")
    settings_display_df = pd.DataFrame(
        [
            {"Setting": "Tracking", "New value": "Disable email open tracking and link click tracking"},
            {"Setting": "Plain text", "New value": "send_as_plain_text = true"},
            {"Setting": "AI ESP matching", "New value": "enable_ai_esp_matching = false"},
        ]
    )
    st.dataframe(settings_display_df, use_container_width=True, hide_index=True)

    if not api_key or not valid_campaign_ids:
        st.stop()

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_files: Dict[str, pd.DataFrame] = {}

    try:
        st.subheader("4. Preview")
        with st.spinner("Fetching campaigns and building preview..."):
            campaigns = fetch_all_campaigns_cached(
                api_key=api_key,
                client_id_filter=client_id_filter,
                request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )

        all_campaigns_df = prepare_campaigns_dataframe(campaigns)
        all_campaigns_snapshot_df = dataframe_flatten_for_csv(compact_campaign_snapshot(all_campaigns_df.copy()))
        output_files["01_all_campaigns_snapshot.csv"] = all_campaigns_snapshot_df

        plan_df, input_campaigns_found_df, subsequence_df, missing_campaign_ids_df = build_update_plan(
            all_campaigns_df=all_campaigns_df,
            valid_campaign_ids=valid_campaign_ids,
            include_parent_campaigns=include_parent_campaigns,
            include_subsequences=include_subsequences,
            skip_archived_campaigns=DEFAULT_SKIP_ARCHIVED_CAMPAIGNS,
        )

        output_files["02_input_campaign_ids.csv"] = pd.DataFrame({"campaign_id": valid_campaign_ids})
        output_files["03_missing_campaign_ids.csv"] = missing_campaign_ids_df

        render_summary_cards(
            valid_campaign_ids=valid_campaign_ids,
            input_found_count=len(input_campaigns_found_df),
            subsequence_count=len(subsequence_df),
            missing_count=len(missing_campaign_ids_df),
            plan_count=len(plan_df),
        )

        if plan_df.empty:
            st.error("Update plan is empty. No campaigns or subsequences matched your selected options.")
            st.stop()

        plan_export_df = dataframe_flatten_for_csv(select_plan_export_columns(plan_df))
        output_files["04_update_plan.csv"] = plan_export_df

        st.dataframe(plan_export_df, use_container_width=True)

        if dry_run:
            st.info("Dry run is on. No Smartlead updates will be sent.")
            summary_df = pd.DataFrame(
                [
                    {"metric": "dry_run", "value": True},
                    {"metric": "input_ids", "value": len(valid_campaign_ids)},
                    {"metric": "input_campaigns_found", "value": len(input_campaigns_found_df)},
                    {"metric": "subsequences_matched", "value": len(subsequence_df)},
                    {"metric": "missing_campaign_ids", "value": len(missing_campaign_ids_df)},
                    {"metric": "total_plan_rows", "value": len(plan_df)},
                ]
            )
            output_files["05_run_summary.csv"] = summary_df

            final_result_df = plan_export_df.copy()
            final_result_df["run_mode"] = "dry_run_plan_only"
            output_files["99_final_result.csv"] = dataframe_flatten_for_csv(final_result_df)

            st.subheader("Downloads")
            st.download_button(
                label="Download preview CSV",
                data=dataframe_to_csv_bytes(output_files["99_final_result.csv"]),
                file_name=f"smartlead_campaign_settings_preview_{run_id}.csv",
                mime="text/csv",
            )
            st.download_button(
                label="Download all outputs as ZIP",
                data=build_zip_bytes(output_files),
                file_name=f"smartlead_campaign_settings_preview_outputs_{run_id}.zip",
                mime="application/zip",
            )
            st.stop()

        st.warning("Dry run is off. Clicking the button below will update Smartlead campaign settings.")
        run_clicked = st.button(
            f"Apply settings to {len(plan_df):,} campaigns",
            type="primary",
        )

        if not run_clicked:
            st.stop()

        st.subheader("5. Live update")
        started = time.time()

        results_df = run_updates_with_progress(
            api_key=api_key,
            plan_df=plan_df,
            concurrency=DEFAULT_CONCURRENCY,
            request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            max_retries=DEFAULT_MAX_RETRIES,
            retry_base_seconds=DEFAULT_RETRY_BASE_SECONDS,
            retry_max_seconds=DEFAULT_RETRY_MAX_SECONDS,
            enable_legacy_tracking_fallback_on_422=DEFAULT_ENABLE_LEGACY_TRACKING_FALLBACK_ON_422,
            include_existing_campaign_name_in_payload=DEFAULT_INCLUDE_EXISTING_CAMPAIGN_NAME_IN_PAYLOAD,
        )
        elapsed_seconds = round(time.time() - started, 2)

        export_results_df = dataframe_flatten_for_csv(results_df.copy())
        output_files["05_update_results.csv"] = export_results_df

        success_count = int(results_df["success"].sum()) if "success" in results_df.columns else 0
        failure_count = int((~results_df["success"]).sum()) if "success" in results_df.columns else len(results_df)

        summary_df = pd.DataFrame(
            [
                {"metric": "total_attempted", "value": len(results_df)},
                {"metric": "successful", "value": success_count},
                {"metric": "failed", "value": failure_count},
                {"metric": "elapsed_seconds", "value": elapsed_seconds},
                {"metric": "dry_run", "value": False},
            ]
        )
        output_files["06_update_summary.csv"] = summary_df

        if failure_count:
            st.warning(f"Live update complete. Attempted {len(results_df):,}, successful {success_count:,}, failed {failure_count:,}.")
        else:
            st.success(f"Live update complete. Attempted {len(results_df):,}, successful {success_count:,}, failed {failure_count:,}.")

        st.dataframe(export_results_df, use_container_width=True)

        failed_df = results_df[~results_df["success"]].copy() if "success" in results_df.columns else pd.DataFrame()
        if not failed_df.empty:
            export_failed_df = dataframe_flatten_for_csv(failed_df.copy())
            output_files["07_failed_updates.csv"] = export_failed_df
            with st.expander(f"Failed updates: {len(export_failed_df):,}", expanded=True):
                st.dataframe(export_failed_df, use_container_width=True)

        final_result_df = plan_export_df.merge(
            export_results_df,
            on="campaign_id",
            how="left",
            suffixes=("_plan", "_update"),
        )

        if DEFAULT_VERIFY_AFTER_LIVE_UPDATE:
            st.subheader("6. Verification")
            with st.spinner("Re-fetching campaigns and verifying settings..."):
                verification_df = verify_updated_settings(
                    api_key=api_key,
                    plan_df=plan_df,
                    client_id_filter=client_id_filter,
                    request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                )

            verification_export_df = dataframe_flatten_for_csv(verification_df.copy())
            output_files["08_verification_after_update.csv"] = verification_export_df

            ok_count = int(verification_df["all_requested_settings_ok"].sum()) if "all_requested_settings_ok" in verification_df.columns else 0
            not_ok_count = int((~verification_df["all_requested_settings_ok"]).sum()) if "all_requested_settings_ok" in verification_df.columns else len(verification_df)

            if not_ok_count:
                st.warning(f"Verification complete. OK: {ok_count:,}. Not OK: {not_ok_count:,}.")
            else:
                st.success(f"Verification complete. OK: {ok_count:,}. Not OK: {not_ok_count:,}.")

            st.dataframe(verification_export_df, use_container_width=True)

            final_result_df = final_result_df.merge(
                verification_export_df,
                on="campaign_id",
                how="left",
                suffixes=("", "_verified"),
            )

        output_files["99_final_result.csv"] = dataframe_flatten_for_csv(final_result_df)

        st.subheader("Downloads")
        st.download_button(
            label="Download final result CSV",
            data=dataframe_to_csv_bytes(output_files["99_final_result.csv"]),
            file_name=f"smartlead_campaign_settings_result_{run_id}.csv",
            mime="text/csv",
        )
        st.download_button(
            label="Download all outputs as ZIP",
            data=build_zip_bytes(output_files),
            file_name=f"smartlead_campaign_settings_outputs_{run_id}.zip",
            mime="application/zip",
        )

    except Exception as exc:
        st.error(str(exc))
        st.exception(exc)


if __name__ == "__main__":
    main()
