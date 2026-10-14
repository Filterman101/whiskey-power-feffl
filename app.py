import os
import re
import math
import time
import base64
from pathlib import Path
from datetime import datetime
from typing import Dict, Tuple, Optional

import requests
import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageOps
from scipy.optimize import milp, LinearConstraint, Bounds

try:
    import nfl_data_py as nfl
except ImportError:
    nfl = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


# ==============================================================================
# 0. AUTOMATIC WHISKEY POWER ICON CROPPER & MOBILE PWA INJECTOR
# ==============================================================================
ICON_PATH = Path("icon.png")
SECRETS_PATH = Path(".streamlit/secrets.toml")
PROFILE_DIR = Path(".cbs_browser_profile")


def prepare_whiskey_power_icon() -> Optional[Path]:
    if ICON_PATH.exists():
        return ICON_PATH
    candidates = [
        p for p in Path(".").iterdir()
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        and p.name.lower() not in {"icon_clean.png"}
    ]
    if not candidates:
        return None
    src_img = candidates[0]
    try:
        img = Image.open(src_img).convert("RGBA")
        w, h = img.size
        if h > w * 1.1:
            img = img.crop((int(w * 0.01), int(h * 0.085), int(w * 0.99), int(h * 0.965)))
        icon_512 = ImageOps.fit(img, (512, 512), method=Image.Resampling.LANCZOS)
        icon_512.save(ICON_PATH, format="PNG")
        return ICON_PATH
    except Exception:
        return src_img


ready_icon = prepare_whiskey_power_icon()
page_icon_obj = Image.open(ready_icon) if (ready_icon and ready_icon.exists()) else "🏈"

st.set_page_config(
    page_title="Whiskey Power FEFFL",
    page_icon=page_icon_obj,
    layout="wide",
    initial_sidebar_state="collapsed"
)


def inject_mobile_app_icon(icon_file: Optional[Path] = ready_icon, app_name: str = "Whiskey Power"):
    if not icon_file or not icon_file.exists():
        return
    b64_icon = base64.b64encode(icon_file.read_bytes()).decode("utf-8")
    data_uri = f"data:image/png;base64,{b64_icon}"
    manifest_json = f"""{{
        "name": "{app_name} - FEFFL Optimizer",
        "short_name": "{app_name}",
        "start_url": ".",
        "display": "standalone",
        "background_color": "#0E1117",
        "theme_color": "#0E1117",
        "icons": [
            {{"src": "{data_uri}", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}}
        ]
    }}"""
    b64_manifest = base64.b64encode(manifest_json.encode("utf-8")).decode("utf-8")
    components.html(
        f"""
        <script>
        (function() {{
            const head = window.parent.document.head;
            head.querySelectorAll("link[rel*='icon'], link[rel='apple-touch-icon'], link[rel='manifest']").forEach(el => el.remove());
            const appleIcon = window.parent.document.createElement('link');
            appleIcon.rel = 'apple-touch-icon';
            appleIcon.sizes = '512x512';
            appleIcon.href = '{data_uri}';
            head.appendChild(appleIcon);
            const fav = window.parent.document.createElement('link');
            fav.rel = 'icon';
            fav.type = 'image/png';
            fav.href = '{data_uri}';
            head.appendChild(fav);
            const manifest = window.parent.document.createElement('link');
            manifest.rel = 'manifest';
            manifest.href = 'data:application/manifest+json;base64,{b64_manifest}';
            head.appendChild(manifest);
        }})();
        </script>
        """,
        height=0,
    )


inject_mobile_app_icon()


# ==============================================================================
# 1. SECRETS HELPER
# ==============================================================================
def read_toml_secrets(path: Path = SECRETS_PATH) -> Dict[str, str]:
    if not path.exists():
        return {}
    secrets: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("#"):
            continue
        match = re.match(r'^([A-Za-z0-9_]+)\s*=\s*["\'](.*)["\']\s*$', line_clean)
        if match:
            secrets[match.group(1)] = match.group(2)
    return secrets


def get_config_secret(key: str, default: str = "") -> str:
    local_secrets = read_toml_secrets()
    if key in local_secrets and local_secrets[key]:
        return local_secrets[key]
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.getenv(key, default)


# ==============================================================================
# 2. CBS FANTASY v3.0 API SYNC CONNECTOR (WITH DIRECT TOKEN SUPPORT)
# ==============================================================================
class CBSFantasySync:
    def __init__(self, league_id: str, my_team_id: str = "1", token_or_cookie: Optional[str] = None):
        clean_lid = league_id.strip().replace("https://", "").replace("http://", "")
        clean_lid = clean_lid.split(".football.cbssports.com")[0].split("/")[0].strip()
        self.league_id = clean_lid
        self.my_team_id = re.sub(r"[^0-9]", "", str(my_team_id)) or "1"
        self.raw_auth = token_or_cookie.strip().strip('"').strip("'") if token_or_cookie else ""
        self.league_domain = f"https://{self.league_id}.football.cbssports.com"
        self.api_base = "https://api.cbssports.com/fantasy"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        })

    @staticmethod
    def normalize_name(name: str) -> str:
        if not isinstance(name, str):
            return ""
        clean = re.sub(r"\b(Jr\.?|Sr\.?|III|II|IV|V)\b", "", name, flags=re.IGNORECASE)
        clean = re.sub(r"[^a-zA-Z\s]", "", clean)
        return " ".join(clean.split()).strip()

    def _resolve_token(self) -> str:
        if not self.raw_auth:
            raise ValueError("Please paste your CBS Access Token into the sidebar box.")

        if "=" in self.raw_auth and "access_token=" not in self.raw_auth:
            for part in self.raw_auth.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    self.session.cookies.set(k.strip(), v.strip(), domain=".cbssports.com")
        elif "access_token=" in self.raw_auth:
            m = re.search(r"access_token=([^&\s\"']+)", self.raw_auth)
            if m:
                return m.group(1)
        else:
            self.session.cookies.set("CBS_SESS", self.raw_auth, domain=".cbssports.com")
            return self.raw_auth

        resp = self.session.get(f"{self.league_domain}/teams", timeout=12)
        for pat in [r'"access_token"\s*:\s*"([^"]+)"', r"access_token\s*=\s*['\"]([^'\"]+)['\"]", r"token\s*:\s*['\"]([a-zA-Z0-9_\-\.]{20,})['\"]"]:
            m = re.search(pat, resp.text)
            if m:
                return m.group(1)
        return self.raw_auth

    def fetch_rosters_and_faab(self, token: str) -> Tuple[pd.DataFrame, Dict]:
        params = {"version": "3.0", "league_id": self.league_id, "access_token": token, "response_format": "json"}
        t_resp = self.session.get(f"{self.api_base}/league/teams", params=params, timeout=12)
        try:
            t_json = t_resp.json()
        except Exception:
            raise ConnectionError(f"CBS API did not return JSON (HTTP {t_resp.status_code}). Check your League ID ({self.league_id}) and Access Token.")

        teams_data = t_json.get("body", {}).get("teams", [])
        faab_list, my_faab = [], 68
        for t in teams_data:
            t_id = str(t.get("id", ""))
            bal = float(t.get("faab_balance", t.get("waiver_budget", 100)))
            faab_list.append(bal)
            if t_id == self.my_team_id:
                my_faab = int(bal)

        params["team_id"] = "all"
        r_resp = self.session.get(f"{self.api_base}/league/rosters", params=params, timeout=12)
        r_json = r_resp.json()
        teams_rosters = r_json.get("body", {}).get("rosters", {}).get("teams", [])
        if not teams_rosters:
            err_msg = r_json.get("statusMessage", "No rosters returned. Verify your Access Token & League ID.")
            raise ConnectionError(f"CBS API Response: {err_msg}")

        rows = []
        valid_pos = {"QB", "RB", "WR", "TE", "K", "DEF", "DST"}
        for team in teams_rosters:
            t_id = str(team.get("id", ""))
            status = "My Roster" if t_id == self.my_team_id else "Opponent Roster"
            for p in team.get("players", []):
                el_pos = p.get("eligible_positions", [p.get("position", "")])
                pos = el_pos[0] if isinstance(el_pos, list) and el_pos else str(p.get("position", ""))
                pos = "DEF" if pos == "DST" else pos
                if pos not in valid_pos:
                    continue
                fname = p.get("fullname", f"{p.get('firstname', '')} {p.get('lastname', '')}").strip()
                rows.append({
                    "Player": fname,
                    "Clean_Name": self.normalize_name(fname),
                    "Pos": pos,
                    "Team": p.get("pro_team", "FA"),
                    "Status": status,
                    "CBS_Proj": float(p.get("projected_points", p.get("ytd_points", 11.5)) or 11.5)
                })

        meta = {
            "my_faab": my_faab,
            "league_avg_faab": int(round(np.mean(faab_list))) if faab_list else 54,
            "teams_synced": len(teams_rosters)
        }
        return pd.DataFrame(rows), meta

    def fetch_waiver_pool(self, token: str, limit: int = 85) -> pd.DataFrame:
        params = {"version": "3.0", "league_id": self.league_id, "status": "free_agent", "access_token": token, "response_format": "json"}
        try:
            resp = self.session.get(f"{self.api_base}/players/list", params=params, timeout=12)
            players = resp.json().get("body", {}).get("players", [])
        except Exception:
            players = []
        rows = []
        valid_pos = {"QB", "RB", "WR", "TE", "K", "DEF", "DST"}
        for p in players[:limit]:
            el_pos = p.get("eligible_positions", [p.get("position", "")])
            pos = el_pos[0] if isinstance(el_pos, list) and el_pos else str(p.get("position", ""))
            pos = "DEF" if pos == "DST" else pos
            if pos not in valid_pos:
                continue
            fname = p.get("fullname", "").strip()
            rows.append({
                "Player": fname,
                "Clean_Name": self.normalize_name(fname),
                "Pos": pos,
                "Team": p.get("pro_team", "FA"),
                "Status": "Waiver",
                "CBS_Proj": float(p.get("projected_points", 8.5) or 8.5)
            })
        return pd.DataFrame(rows)

    def fetch_draft_results(self, token: str) -> pd.DataFrame:
        params = {"version": "3.0", "league_id": self.league_id, "access_token": token, "response_format": "json"}
        try:
            resp = self.session.get(f"{self.api_base}/league/draft/results", params=params, timeout=12)
            picks = resp.json().get("body", {}).get("draft_results", {}).get("picks", [])
        except Exception:
            picks = []
        rows = []
        for p in picks:
            p_obj = p.get("player", {})
            fname = p_obj.get("fullname", "").strip()
            el_pos = p_obj.get("eligible_positions", [p_obj.get("position", "")])
            pos = el_pos[0] if isinstance(el_pos, list) and el_pos else str(p_obj.get("position", ""))
            pos = "DEF" if pos == "DST" else pos
            rows.append({"Clean_Name": self.normalize_name(fname), "Pos": pos, "Draft_Round": int(p.get("round", 12))})
        if not rows:
            return pd.DataFrame(columns=["Clean_Name", "Pos", "Draft_Round"])
        return pd.DataFrame(rows).drop_duplicates(subset=["Clean_Name", "Pos"], keep="first")

    def sync_all(self, usage_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict]:
        token = self._resolve_token()
        rosters_df, faab_meta = self.fetch_rosters_and_faab(token)
        waivers_df = self.fetch_waiver_pool(token, limit=85)
        draft_df = self.fetch_draft_results(token)

        combined = pd.concat([rosters_df, waivers_df], ignore_index=True)
        usage_clean = usage_df.copy()
        if "Clean_Name" not in usage_clean.columns:
            usage_clean["Clean_Name"] = usage_clean["Player"].apply(self.normalize_name)

        merged = pd.merge(
            combined,
            usage_clean.drop(columns=["Player", "Team", "Status", "Draft_Round", "player_id"], errors="ignore"),
            on=["Clean_Name", "Pos"], how="left"
        )
        merged = pd.merge(merged, draft_df, on=["Clean_Name", "Pos"], how="left")
        merged["Draft_Round"] = merged["Draft_Round"].fillna(12).astype(int)

        merged["Opp"] = merged.get("Opp", pd.Series("TBD", index=merged.index)).fillna("TBD")
        merged["Vegas_Implied"] = merged.get("Vegas_Implied", pd.Series(21.5, index=merged.index)).fillna(21.5)
        merged["Proj_Pts"] = merged.get("Proj_Pts", merged["CBS_Proj"]).fillna(merged["CBS_Proj"]).round(1)
        merged["ROS_PPG"] = merged.get("ROS_PPG", merged["Proj_Pts"] * 0.96).fillna(merged["Proj_Pts"] * 0.96).round(1)
        default_rec = merged["Pos"].map({"WR": 3.5, "TE": 3.5, "RB": 2.2}).fillna(0.0)
        merged["Proj_Rec"] = merged.get("Proj_Rec", pd.Series(np.nan, index=merged.index)).fillna(default_rec).round(1)
        merged["Floor"] = merged.get("Floor", merged["Proj_Pts"] * 0.62).fillna(merged["Proj_Pts"] * 0.62).round(1)
        merged["Ceiling"] = merged.get("Ceiling", merged["Proj_Pts"] * 1.48).fillna(merged["Proj_Pts"] * 1.48).round(1)
        merged["Snap_%"] = merged.get("Snap_%", pd.Series(65, index=merged.index)).fillna(65).astype(int)
        merged["Snap_Trend_2W"] = merged.get("Snap_Trend_2W", pd.Series(2.5, index=merged.index)).fillna(2.5).round(1)
        merged["Target_%"] = merged.get("Target_%", pd.Series(14.0, index=merged.index)).fillna(14.0).round(1)
        merged["Air_Yards_%"] = merged.get("Air_Yards_%", pd.Series(12.0, index=merged.index)).fillna(12.0).round(1)
        merged["WOPR"] = merged.get("WOPR", pd.Series(0.32, index=merged.index)).fillna(0.32).round(2)
        merged["DvP_Rank"] = merged.get("DvP_Rank", pd.Series(16, index=merged.index)).fillna(16).astype(int)
        merged["DvP_Diff_Pct"] = merged.get("DvP_Diff_Pct", pd.Series(0.0, index=merged.index)).fillna(0.0).round(1)
        merged["DvP_Mult"] = merged.get("DvP_Mult", pd.Series(1.00, index=merged.index)).fillna(1.00).round(3)
        merged["DvP_Tier"] = merged.get("DvP_Tier", pd.Series("🟡 Neutral", index=merged.index)).fillna("🟡 Neutral")
        merged["Breakout_Index"] = merged.get("Breakout_Index", (merged["WOPR"] * 45.0 + merged["Snap_%"] * 0.35)).fillna(32.0).round(1)

        for col, default in [
            ("Playoff_Slate", "TBD → TBD → TBD"), ("Wk15_Mult", 1.00), ("Wk16_Mult", 1.00),
            ("Wk17_Mult", 1.00), ("Playoff_Mult", 1.00), ("Playoff_Boost_Pct", 0.0),
            ("Playoff_SOS_Rank", 16), ("Playoff_Tier", "⚖️ Neutral Slate"),
            ("Opp_Vegas_Implied", 21.5), ("Opp_Sack_Rate_Pct", 7.2), ("Proj_Sacks", 2.6), ("Proj_TO", 1.3),
            ("WkN1_Opp", "TBD"), ("WkN1_Opp_Implied", 21.5), ("WkN1_Opp_Sack_Rate_Pct", 7.2),
            ("WkN1_Proj_Pts", 8.0), ("Two_Wk_Proj_Pts", 16.0), ("Stash_Tag", "🔥 2-Week Smash Hold"),
            ("Team_Spread", 0.0), ("Roof", "outdoors"), ("Is_Dome", False), ("Proj_FGs", 1.9)
        ]:
            merged[col] = merged[col].fillna(default) if col in merged.columns else default

        return merged, faab_meta


# ==============================================================================
# 3. FALLBACK SAMPLE DATA & STATE INITIALIZATION
# ==============================================================================
if "synced_df" not in st.session_state:
    st.session_state["synced_df"] = None
if "my_faab" not in st.session_state:
    st.session_state["my_faab"] = 68
if "opp_avg_faab" not in st.session_state:
    st.session_state["opp_avg_faab"] = 54


@st.cache_data
def load_fallback_sample_data() -> pd.DataFrame:
    rows = [
        {"Player": "Jayden Daniels", "Pos": "QB", "Team": "WAS", "Status": "My Roster", "Draft_Round": 9, "Opp": "BAL", "Vegas_Implied": 26.5, "Proj_Rec": 0.0, "Proj_Pts": 21.8, "ROS_PPG": 21.4, "Floor": 15.4, "Ceiling": 29.8, "Snap_%": 100, "Snap_Trend_2W": 0.0, "Target_%": 0.0, "Air_Yards_%": 0.0, "WOPR": 0.0, "DvP_Rank": 24, "DvP_Diff_Pct": 8.5, "DvP_Mult": 1.05, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 1.08, "Wk16_Mult": 0.94, "Wk17_Mult": 1.11, "Playoff_Slate": "@NO → PHI → ATL", "Playoff_SOS_Rank": 26, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "Caleb Williams", "Pos": "QB", "Team": "CHI", "Status": "My Roster", "Draft_Round": 10, "Opp": "JAX", "Vegas_Implied": 24.0, "Proj_Rec": 0.0, "Proj_Pts": 18.6, "ROS_PPG": 17.9, "Floor": 11.2, "Ceiling": 26.5, "Snap_%": 100, "Snap_Trend_2W": 0.0, "Target_%": 0.0, "Air_Yards_%": 0.0, "WOPR": 0.0, "DvP_Rank": 29, "DvP_Diff_Pct": 14.2, "DvP_Mult": 1.08, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 0.95, "Wk16_Mult": 1.04, "Wk17_Mult": 1.02, "Playoff_Slate": "@MIN → DET → SEA", "Playoff_SOS_Rank": 17, "Playoff_Tier": "⚖️ Neutral Slate"},
        {"Player": "Breece Hall", "Pos": "RB", "Team": "NYJ", "Status": "My Roster", "Draft_Round": 1, "Opp": "BUF", "Vegas_Implied": 20.5, "Proj_Rec": 4.8, "Proj_Pts": 17.9, "ROS_PPG": 18.2, "Floor": 11.5, "Ceiling": 25.8, "Snap_%": 78, "Snap_Trend_2W": 4.5, "Target_%": 16.5, "Air_Yards_%": 4.2, "WOPR": 0.28, "DvP_Rank": 27, "DvP_Diff_Pct": 11.0, "DvP_Mult": 1.06, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 1.09, "Wk16_Mult": 1.05, "Wk17_Mult": 0.98, "Playoff_Slate": "@JAX → LAR → @BUF", "Playoff_SOS_Rank": 25, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "Chase Brown", "Pos": "RB", "Team": "CIN", "Status": "My Roster", "Draft_Round": 10, "Opp": "@NYG", "Vegas_Implied": 25.0, "Proj_Rec": 3.6, "Proj_Pts": 14.8, "ROS_PPG": 14.2, "Floor": 9.6, "Ceiling": 21.4, "Snap_%": 66, "Snap_Trend_2W": 14.5, "Target_%": 12.4, "Air_Yards_%": 2.1, "WOPR": 0.20, "DvP_Rank": 22, "DvP_Diff_Pct": 5.4, "DvP_Mult": 1.03, "DvP_Tier": "🟡 Neutral", "Wk15_Mult": 0.92, "Wk16_Mult": 0.90, "Wk17_Mult": 0.96, "Playoff_Slate": "@TEN → CLE → DEN", "Playoff_SOS_Rank": 6, "Playoff_Tier": "❄️ Playoff Landmine (Bottom-8)"},
        {"Player": "Tyjae Spears", "Pos": "RB", "Team": "TEN", "Status": "My Roster", "Draft_Round": 9, "Opp": "IND", "Vegas_Implied": 19.5, "Proj_Rec": 4.1, "Proj_Pts": 11.6, "ROS_PPG": 10.9, "Floor": 7.8, "Ceiling": 16.4, "Snap_%": 52, "Snap_Trend_2W": 3.0, "Target_%": 14.2, "Air_Yards_%": 3.0, "WOPR": 0.23, "DvP_Rank": 28, "DvP_Diff_Pct": 12.5, "DvP_Mult": 1.07, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 1.04, "Wk16_Mult": 1.06, "Wk17_Mult": 1.03, "Playoff_Slate": "CIN → @IND → @JAX", "Playoff_SOS_Rank": 23, "Playoff_Tier": "⚖️ Neutral Slate"},
        {"Player": "Amon-Ra St. Brown", "Pos": "WR", "Team": "DET", "Status": "My Roster", "Draft_Round": 1, "Opp": "@DAL", "Vegas_Implied": 27.5, "Proj_Rec": 7.9, "Proj_Pts": 21.2, "ROS_PPG": 20.4, "Floor": 14.8, "Ceiling": 28.5, "Snap_%": 92, "Snap_Trend_2W": 1.5, "Target_%": 29.5, "Air_Yards_%": 31.0, "WOPR": 0.66, "DvP_Rank": 25, "DvP_Diff_Pct": 9.2, "DvP_Mult": 1.05, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 1.02, "Wk16_Mult": 1.08, "Wk17_Mult": 1.05, "Playoff_Slate": "BUF → @CHI → @SF", "Playoff_SOS_Rank": 24, "Playoff_Tier": "⚖️ Neutral Slate"},
        {"Player": "Wan'Dale Robinson", "Pos": "WR", "Team": "NYG", "Status": "My Roster", "Draft_Round": 13, "Opp": "CIN", "Vegas_Implied": 22.0, "Proj_Rec": 7.1, "Proj_Pts": 14.2, "ROS_PPG": 13.4, "Floor": 10.6, "Ceiling": 18.1, "Snap_%": 78, "Snap_Trend_2W": 6.5, "Target_%": 26.8, "Air_Yards_%": 16.5, "WOPR": 0.52, "DvP_Rank": 26, "DvP_Diff_Pct": 10.4, "DvP_Mult": 1.06, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 0.91, "Wk16_Mult": 1.06, "Wk17_Mult": 0.97, "Playoff_Slate": "BAL → @ATL → IND", "Playoff_SOS_Rank": 12, "Playoff_Tier": "⚖️ Neutral Slate"},
        {"Player": "Rashid Shaheed", "Pos": "WR", "Team": "NO", "Status": "My Roster", "Draft_Round": 10, "Opp": "TB", "Vegas_Implied": 20.0, "Proj_Rec": 3.4, "Proj_Pts": 12.6, "ROS_PPG": 12.6, "Floor": 4.2, "Ceiling": 24.8, "Snap_%": 81, "Snap_Trend_2W": 2.0, "Target_%": 18.5, "Air_Yards_%": 41.2, "WOPR": 0.57, "DvP_Rank": 21, "DvP_Diff_Pct": 4.0, "DvP_Mult": 1.02, "DvP_Tier": "🟡 Neutral", "Wk15_Mult": 1.09, "Wk16_Mult": 0.98, "Wk17_Mult": 1.04, "Playoff_Slate": "WAS → @GB → LV", "Playoff_SOS_Rank": 22, "Playoff_Tier": "⚖️ Neutral Slate"},
        {"Player": "Xavier Worthy", "Pos": "WR", "Team": "KC", "Status": "My Roster", "Draft_Round": 8, "Opp": "@SF", "Vegas_Implied": 24.5, "Proj_Rec": 3.8, "Proj_Pts": 12.3, "ROS_PPG": 12.8, "Floor": 5.5, "Ceiling": 23.0, "Snap_%": 68, "Snap_Trend_2W": 8.0, "Target_%": 16.0, "Air_Yards_%": 28.5, "WOPR": 0.44, "DvP_Rank": 11, "DvP_Diff_Pct": -6.5, "DvP_Mult": 0.96, "DvP_Tier": "🟡 Neutral", "Wk15_Mult": 0.94, "Wk16_Mult": 1.02, "Wk17_Mult": 0.95, "Playoff_Slate": "@CLE → HOU → @PIT", "Playoff_SOS_Rank": 10, "Playoff_Tier": "⚖️ Neutral Slate"},
        {"Player": "Trey McBride", "Pos": "TE", "Team": "ARI", "Status": "My Roster", "Draft_Round": 4, "Opp": "@GB", "Vegas_Implied": 23.0, "Proj_Rec": 6.4, "Proj_Pts": 14.9, "ROS_PPG": 14.9, "Floor": 10.2, "Ceiling": 20.6, "Snap_%": 94, "Snap_Trend_2W": 2.0, "Target_%": 25.4, "Air_Yards_%": 22.0, "WOPR": 0.54, "DvP_Rank": 20, "DvP_Diff_Pct": 3.2, "DvP_Mult": 1.02, "DvP_Tier": "🟡 Neutral", "Wk15_Mult": 1.06, "Wk16_Mult": 1.12, "Wk17_Mult": 1.05, "Playoff_Slate": "NE → @CAR → @LAR", "Playoff_SOS_Rank": 29, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "Brandon Aubrey", "Pos": "K", "Team": "DAL", "Status": "My Roster", "Draft_Round": 15, "Opp": "DET", "Vegas_Implied": 24.5, "Team_Spread": 3.0, "Roof": "dome", "Is_Dome": True, "Proj_FGs": 2.2, "Proj_Rec": 0.0, "Proj_Pts": 10.2, "ROS_PPG": 9.5, "Floor": 6.0, "Ceiling": 15.0, "Snap_%": 100, "Snap_Trend_2W": 0.0, "Target_%": 0.0, "Air_Yards_%": 0.0, "WOPR": 0.0, "DvP_Rank": 28, "DvP_Diff_Pct": 11.4, "DvP_Mult": 1.07, "DvP_Tier": "🟢 Dome Smash (24+ Pts)"},
        {"Player": "Broncos DST", "Pos": "DEF", "Team": "DEN", "Status": "My Roster", "Draft_Round": 16, "Opp": "LAC", "Opp_Vegas_Implied": 18.0, "Vegas_Implied": 18.0, "Opp_Sack_Rate_Pct": 8.4, "Proj_Sacks": 3.1, "Proj_TO": 1.5, "Proj_Rec": 0.0, "Proj_Pts": 9.8, "ROS_PPG": 8.4, "Floor": 5.5, "Ceiling": 15.5, "WkN1_Opp": "@NO", "WkN1_Opp_Implied": 17.5, "WkN1_Opp_Sack_Rate_Pct": 9.6, "WkN1_Proj_Pts": 10.6, "Two_Wk_Proj_Pts": 20.4, "Stash_Tag": "🔥 2-Week Smash Hold", "DvP_Tier": "🔥 2-Week Smash Hold"},
        {"Player": "Tyrone Tracy Jr.", "Pos": "RB", "Team": "NYG", "Status": "Waiver", "Draft_Round": 14, "Opp": "CIN", "Vegas_Implied": 22.0, "Proj_Rec": 4.2, "Proj_Pts": 14.6, "ROS_PPG": 13.9, "Floor": 8.8, "Ceiling": 21.5, "Snap_%": 74, "Snap_Trend_2W": 24.5, "Target_%": 15.8, "Air_Yards_%": 3.5, "WOPR": 0.26, "DvP_Rank": 29, "DvP_Diff_Pct": 14.5, "DvP_Mult": 1.08, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 0.94, "Wk16_Mult": 1.09, "Wk17_Mult": 1.11, "Playoff_Slate": "BAL → @ATL → IND", "Playoff_SOS_Rank": 27, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "Kimani Vidal", "Pos": "RB", "Team": "LAC", "Status": "Waiver", "Draft_Round": 15, "Opp": "@DEN", "Vegas_Implied": 21.0, "Proj_Rec": 3.2, "Proj_Pts": 10.6, "ROS_PPG": 10.8, "Floor": 6.2, "Ceiling": 17.5, "Snap_%": 48, "Snap_Trend_2W": 18.0, "Target_%": 11.5, "Air_Yards_%": 2.0, "WOPR": 0.19, "DvP_Rank": 12, "DvP_Diff_Pct": -5.0, "DvP_Mult": 0.97, "DvP_Tier": "🟡 Neutral", "Wk15_Mult": 1.08, "Wk16_Mult": 1.02, "Wk17_Mult": 1.09, "Playoff_Slate": "TB → DEN → @NE", "Playoff_SOS_Rank": 26, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "DeMario Douglas", "Pos": "WR", "Team": "NE", "Status": "Waiver", "Draft_Round": 12, "Opp": "HOU", "Vegas_Implied": 18.5, "Proj_Rec": 5.9, "Proj_Pts": 12.8, "ROS_PPG": 12.5, "Floor": 8.6, "Ceiling": 18.2, "Snap_%": 79, "Snap_Trend_2W": 11.2, "Target_%": 24.8, "Air_Yards_%": 21.4, "WOPR": 0.52, "DvP_Rank": 23, "DvP_Diff_Pct": 6.8, "DvP_Mult": 1.04, "DvP_Tier": "🟡 Neutral", "Wk15_Mult": 1.11, "Wk16_Mult": 1.03, "Wk17_Mult": 1.07, "Playoff_Slate": "@ARI → @BUF → LAC", "Playoff_SOS_Rank": 28, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "Jalen Tolbert", "Pos": "WR", "Team": "DAL", "Status": "Waiver", "Draft_Round": 14, "Opp": "DET", "Vegas_Implied": 24.5, "Proj_Rec": 4.4, "Proj_Pts": 12.4, "ROS_PPG": 11.6, "Floor": 6.8, "Ceiling": 19.6, "Snap_%": 86, "Snap_Trend_2W": 9.5, "Target_%": 18.2, "Air_Yards_%": 26.5, "WOPR": 0.46, "DvP_Rank": 27, "DvP_Diff_Pct": 11.2, "DvP_Mult": 1.06, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 1.12, "Wk16_Mult": 1.10, "Wk17_Mult": 0.96, "Playoff_Slate": "@CAR → TB → @PHI", "Playoff_SOS_Rank": 30, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "Cade Otton", "Pos": "TE", "Team": "TB", "Status": "Waiver", "Draft_Round": 15, "Opp": "@NO", "Vegas_Implied": 23.5, "Proj_Rec": 4.9, "Proj_Pts": 10.6, "ROS_PPG": 10.4, "Floor": 6.8, "Ceiling": 15.4, "Snap_%": 91, "Snap_Trend_2W": 5.5, "Target_%": 18.0, "Air_Yards_%": 14.2, "WOPR": 0.37, "DvP_Rank": 21, "DvP_Diff_Pct": 4.1, "DvP_Mult": 1.02, "DvP_Tier": "🟡 Neutral", "Wk15_Mult": 1.03, "Wk16_Mult": 1.09, "Wk17_Mult": 1.11, "Playoff_Slate": "@LAC → @DAL → CAR", "Playoff_SOS_Rank": 28, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "Chargers DST", "Pos": "DEF", "Team": "LAC", "Status": "Waiver", "Draft_Round": 16, "Opp": "@DEN", "Opp_Vegas_Implied": 19.5, "Vegas_Implied": 19.5, "Opp_Sack_Rate_Pct": 8.9, "Proj_Sacks": 3.3, "Proj_TO": 1.6, "Proj_Rec": 0.0, "Proj_Pts": 9.4, "ROS_PPG": 8.1, "Floor": 5.0, "Ceiling": 15.2, "WkN1_Opp": "@ARI", "WkN1_Opp_Implied": 18.5, "WkN1_Opp_Sack_Rate_Pct": 9.1, "WkN1_Proj_Pts": 9.9, "Two_Wk_Proj_Pts": 19.3, "Stash_Tag": "🔥 2-Week Smash Hold", "DvP_Tier": "🔥 2-Week Smash Hold"},
        {"Player": "Bills DST", "Pos": "DEF", "Team": "BUF", "Status": "Waiver", "Draft_Round": 16, "Opp": "@NYJ", "Opp_Vegas_Implied": 21.0, "Vegas_Implied": 21.0, "Opp_Sack_Rate_Pct": 7.5, "Proj_Sacks": 2.7, "Proj_TO": 1.4, "Proj_Rec": 0.0, "Proj_Pts": 7.9, "ROS_PPG": 8.3, "Floor": 4.0, "Ceiling": 13.5, "WkN1_Opp": "TEN", "WkN1_Opp_Implied": 16.0, "WkN1_Opp_Sack_Rate_Pct": 10.8, "WkN1_Proj_Pts": 11.8, "Two_Wk_Proj_Pts": 19.7, "Stash_Tag": "💎 $0 FAAB Pre-Stash (Wk N+1)", "DvP_Tier": "💎 $0 FAAB Pre-Stash (Wk N+1)"},
        {"Player": "Wil Lutz", "Pos": "K", "Team": "DEN", "Status": "Waiver", "Draft_Round": 16, "Opp": "LAC", "Vegas_Implied": 24.0, "Team_Spread": -3.0, "Roof": "outdoors", "Is_Dome": False, "Proj_FGs": 2.1, "Proj_Rec": 0.0, "Proj_Pts": 9.6, "ROS_PPG": 8.6, "Floor": 5.5, "Ceiling": 14.2, "Snap_%": 100, "Snap_Trend_2W": 0.0, "Target_%": 0.0, "Air_Yards_%": 0.0, "WOPR": 0.0, "DvP_Rank": 25, "DvP_Diff_Pct": 9.1, "DvP_Mult": 1.05, "DvP_Tier": "🟢 High Implied (23.5+ Pts)"},
        {"Player": "Jahmyr Gibbs", "Pos": "RB", "Team": "DET", "Status": "Opponent Roster", "Draft_Round": 1, "Opp": "@DAL", "Vegas_Implied": 27.5, "Proj_Rec": 4.6, "Proj_Pts": 19.6, "ROS_PPG": 19.5, "Floor": 13.5, "Ceiling": 27.5, "Snap_%": 68, "Snap_Trend_2W": 4.0, "Target_%": 15.5, "Air_Yards_%": 3.2, "WOPR": 0.25, "DvP_Rank": 26, "DvP_Diff_Pct": 10.0, "DvP_Mult": 1.06, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 1.09, "Wk16_Mult": 1.12, "Wk17_Mult": 1.06, "Playoff_Slate": "BUF → @CHI → @SF", "Playoff_SOS_Rank": 30, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "Drake London", "Pos": "WR", "Team": "ATL", "Status": "Opponent Roster", "Draft_Round": 2, "Opp": "@CAR", "Vegas_Implied": 25.5, "Proj_Rec": 6.6, "Proj_Pts": 18.2, "ROS_PPG": 17.8, "Floor": 11.8, "Ceiling": 25.4, "Snap_%": 93, "Snap_Trend_2W": 3.5, "Target_%": 28.4, "Air_Yards_%": 33.5, "WOPR": 0.66, "DvP_Rank": 29, "DvP_Diff_Pct": 14.0, "DvP_Mult": 1.08, "DvP_Tier": "🟢 Smash", "Wk15_Mult": 1.11, "Wk16_Mult": 1.13, "Wk17_Mult": 1.14, "Playoff_Slate": "@LV → NYG → @WAS", "Playoff_SOS_Rank": 32, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
        {"Player": "Kyren Williams", "Pos": "RB", "Team": "LAR", "Status": "Opponent Roster", "Draft_Round": 2, "Opp": "LV", "Vegas_Implied": 24.0, "Proj_Rec": 3.4, "Proj_Pts": 18.1, "ROS_PPG": 18.1, "Floor": 12.5, "Ceiling": 24.5, "Snap_%": 88, "Snap_Trend_2W": 1.0, "Target_%": 11.2, "Air_Yards_%": 1.8, "WOPR": 0.18, "DvP_Rank": 24, "DvP_Diff_Pct": 8.0, "DvP_Mult": 1.04, "DvP_Tier": "🟡 Neutral", "Wk15_Mult": 0.96, "Wk16_Mult": 1.08, "Wk17_Mult": 1.11, "Playoff_Slate": "@SF → @NYJ → ARI", "Playoff_SOS_Rank": 24, "Playoff_Tier": "⚖️ Neutral Slate"},
        {"Player": "Garrett Wilson", "Pos": "WR", "Team": "NYJ", "Status": "Opponent Roster", "Draft_Round": 1, "Opp": "BUF", "Vegas_Implied": 20.5, "Proj_Rec": 6.8, "Proj_Pts": 16.9, "ROS_PPG": 17.2, "Floor": 10.8, "Ceiling": 25.2, "Snap_%": 96, "Snap_Trend_2W": 0.5, "Target_%": 30.1, "Air_Yards_%": 36.0, "WOPR": 0.70, "DvP_Rank": 18, "DvP_Diff_Pct": 1.2, "DvP_Mult": 1.01, "DvP_Tier": "🟡 Neutral", "Wk15_Mult": 1.12, "Wk16_Mult": 1.07, "Wk17_Mult": 0.96, "Playoff_Slate": "@JAX → LAR → @BUF", "Playoff_SOS_Rank": 27, "Playoff_Tier": "🏆 League-Winner Slate (Top-8)"},
    ]
    sample_df = pd.DataFrame(rows)
    defaults = {
        "Wk15_Mult": 1.00, "Wk16_Mult": 1.00, "Wk17_Mult": 1.00,
        "Playoff_Slate": "TBD → TBD → TBD", "Playoff_SOS_Rank": 16, "Playoff_Tier": "⚖️ Neutral Slate",
        "Opp_Vegas_Implied": 21.5, "Opp_Sack_Rate_Pct": 7.2, "Proj_Sacks": 2.6, "Proj_TO": 1.3,
        "WkN1_Opp": "TBD", "WkN1_Opp_Implied": 21.5, "WkN1_Opp_Sack_Rate_Pct": 7.2,
        "WkN1_Proj_Pts": 7.5, "Two_Wk_Proj_Pts": 15.0, "Stash_Tag": "N/A",
        "Team_Spread": 0.0, "Roof": "outdoors", "Is_Dome": False, "Proj_FGs": 1.8
    }
    for col, val in defaults.items():
        sample_df[col] = sample_df[col].fillna(val) if col in sample_df.columns else val
    sample_df["Breakout_Index"] = (
        (sample_df["WOPR"] * 45.0) + (sample_df["Snap_%"] * 0.35) + np.maximum(0, sample_df["Snap_Trend_2W"] * 1.2)
    ).round(1)
    return sample_df


# ==============================================================================
# 4. SIDEBAR CONTROLS & LIVE CBS SYNC
# ==============================================================================
st.sidebar.title("🥃 Whiskey Power Control")
current_week = st.sidebar.slider("Current NFL Week", min_value=1, max_value=17, value=7)
strategy = st.sidebar.radio(
    "Matchup Game Script (MILP Mode):",
    ["Balanced (Max EV)", "Protect Lead (High Floor)", "Chasing Points (High Ceiling)"],
)
lam = {"Balanced (Max EV)": 0.0, "Protect Lead (High Floor)": -0.35, "Chasing Points (High Ceiling)": 0.50}[strategy]

with st.sidebar.expander("🔐 CBS Live League Sync", expanded=True):
    cbs_league_id = st.text_input("CBS League ID", value=get_config_secret("CBS_LEAGUE_ID", "feffl"))
    cbs_team_id = st.text_input("Your CBS Team ID (1-14)", value=get_config_secret("CBS_TEAM_ID", "14"))
    manual_token = st.text_input("CBS Access Token (from Ctrl+U)", value=get_config_secret("CBS_SESS", ""), type="password")

    if st.button("🚀 Sync CBS League Now", use_container_width=True):
        with st.spinner("Syncing 14-team CBS rosters, FAAB & Waivers..."):
            try:
                auth_val = (manual_token or get_config_secret("CBS_SESS", "")).strip()
                base_usage_df = load_fallback_sample_data()
                connector = CBSFantasySync(cbs_league_id, cbs_team_id, auth_val)
                synced_df, faab_meta = connector.sync_all(base_usage_df)
                st.session_state["synced_df"] = synced_df
                st.session_state["my_faab"] = faab_meta["my_faab"]
                st.session_state["opp_avg_faab"] = faab_meta["league_avg_faab"]
                st.success(f"✅ Synced {faab_meta['teams_synced']} teams! Your FAAB: ${faab_meta['my_faab']}")
            except Exception as e:
                st.error(f"Sync error: {e}")

    uploaded_csv = st.file_uploader("Or Drop CBS / FantasyPros CSV", type=["csv"])
    if uploaded_csv is not None:
        st.session_state["synced_df"] = pd.read_csv(uploaded_csv)
        st.success("✅ Loaded custom league CSV!")

with st.sidebar.expander("💰 FAAB & Keeper Settings", expanded=False):
    faab_start = st.number_input("Starting Season FAAB ($)", value=100, step=10)
    faab_rem = st.number_input("Your Remaining FAAB ($)", value=int(st.session_state["my_faab"]), min_value=0, max_value=500)
    opp_avg_faab = st.number_input("League Avg Remaining FAAB ($)", value=int(st.session_state["opp_avg_faab"]), min_value=0, max_value=500)
    reserve_per_week = st.slider("Streaming Reserve ($/Week)", 0.0, 3.0, 1.0, 0.5)
    playoff_weight = st.slider("Playoff SOS Weight (θ):", 0.0, 1.0, 0.45, 0.05)
    has_bye_lock = st.checkbox("Projecting Top-2 Seed (Week 15 Bye)?", value=False)
    round_penalty = st.number_input("Keeper Round Escalator Penalty", min_value=0, max_value=3, value=0)
    waivers_eligible = st.checkbox("Undrafted Waivers Keeper-Eligible (Rd 12)?", value=True)


# ==============================================================================
# 5. QUANTITATIVE ENRICHMENT & MILP LINEUP SOLVER (CBS SLOT ORDER)
# ==============================================================================
df = (st.session_state["synced_df"] if st.session_state["synced_df"] is not None else load_fallback_sample_data()).copy()

for m_col in ["Wk15_Mult", "Wk16_Mult", "Wk17_Mult"]:
    df[m_col] = df.get(m_col, pd.Series(1.00, index=df.index)).fillna(1.00)

df["Playoff_Mult"] = (
    (0.55 * df["Wk16_Mult"] + 0.45 * df["Wk17_Mult"]) if has_bye_lock
    else (0.40 * df["Wk15_Mult"] + 0.35 * df["Wk16_Mult"] + 0.25 * df["Wk17_Mult"])
).round(3)
df["Playoff_Boost_Pct"] = ((df["Playoff_Mult"] - 1.0) * 100.0).round(1)
df["Playoff_PPG"] = (df["ROS_PPG"] * df["Playoff_Mult"]).round(1)

replacement_baseline = {"QB": 15.5, "RB": 8.2, "WR": 9.0, "TE": 7.5, "K": 7.5, "DEF": 6.5}
scarcity_mult = {"QB": 0.85, "RB": 1.22, "WR": 1.15, "TE": 1.10, "K": 0.50, "DEF": 0.60}

df["Std_Dev"] = ((df["Ceiling"] - df["Floor"]) / 2.56).round(2)
df["Opt_Score"] = (df["Proj_Pts"] + (lam * df["Std_Dev"])).round(2)
df["VORP_14T"] = np.maximum(0.5, (df["ROS_PPG"] - df["Pos"].map(replacement_baseline).fillna(8.0)) * df["Pos"].map(scarcity_mult).fillna(1.0)).round(2)
df["Playoff_VORP_14T"] = np.maximum(0.5, (df["Playoff_PPG"] - df["Pos"].map(replacement_baseline).fillna(8.0)) * df["Pos"].map(scarcity_mult).fillna(1.0)).round(2)
df["Effective_VORP"] = ((1.0 - playoff_weight) * df["VORP_14T"] + (playoff_weight * df["Playoff_VORP_14T"])).round(2)
df["Trade_Value"] = ((df["Effective_VORP"] ** 1.28) * 2.5).round(1)

df["Draft_Round"] = df.get("Draft_Round", pd.Series(12, index=df.index)).fillna(12).astype(int)
df["Was_Drafted"] = df["Draft_Round"] <= 16
df["Keeper_Cost_Rd"] = np.maximum(1, df["Draft_Round"] - round_penalty)
df["Keeper_Eligible"] = (df["Draft_Round"] >= 8) & (df["Pos"].isin(["QB", "RB", "WR", "TE"])) & (True if waivers_eligible else df["Was_Drafted"])
df["Proj_Next_Yr_Rd"] = np.clip(np.round(11.5 - (df["VORP_14T"] * 1.15)), 1, 15).astype(int)

val_proj = 95.0 * np.exp(-0.21 * (df["Proj_Next_Yr_Rd"] - 1))
val_cost = 95.0 * np.exp(-0.21 * (df["Keeper_Cost_Rd"] - 1))
df["Keeper_Surplus"] = np.where(df["Keeper_Eligible"] & (df["Proj_Next_Yr_Rd"] < df["Keeper_Cost_Rd"]), val_proj - val_cost, 0.0).round(1)
df["Keeper_Tag"] = np.where(
    ~df["Keeper_Eligible"], "🚫 Ineligible (Rd 1-7)",
    np.where(df["Keeper_Surplus"] >= 35.0, "👑 Elite Keeper (Rd " + df["Keeper_Cost_Rd"].astype(str) + ")",
             np.where(df["Keeper_Surplus"] >= 15.0, "🔒 Solid Keeper (Rd " + df["Keeper_Cost_Rd"].astype(str) + ")", "⚪ No Surplus"))
)

CBS_POS_ORDER = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "DEF": 6}


def optimize_lineup(roster_df: pd.DataFrame, score_col: str = "Opt_Score") -> pd.DataFrame:
    out = roster_df.copy()
    n = len(out)
    if n == 0:
        out["Decision"] = []
        out["Slot"] = []
        out["Slot_Order"] = []
        return out
    c = -out[score_col].fillna(0.0).values
    A = np.vstack([
        (out["Pos"] == "QB").astype(int).values,
        (out["Pos"] == "RB").astype(int).values,
        (out["Pos"] == "WR").astype(int).values,
        (out["Pos"] == "TE").astype(int).values,
        out["Pos"].isin(["RB", "WR", "TE"]).astype(int).values,
        (out["Pos"] == "K").astype(int).values,
        (out["Pos"] == "DEF").astype(int).values,
    ])
    res = milp(c=c, integrality=np.ones(n), bounds=Bounds(0, 1), constraints=LinearConstraint(A, [1, 2, 2, 1, 6, 1, 1], [1, 3, 3, 2, 6, 1, 1]))
    if res.success and res.x is not None:
        out["Decision"] = ["START" if val > 0.5 else "SIT" for val in res.x]
    else:
        out = out.sort_values(by=score_col, ascending=False)
        out["Decision"] = "SIT"
        out.iloc[:min(9, len(out)), out.columns.get_loc("Decision")] = "START"

    out["Slot"] = out["Pos"]
    out["Slot_Order"] = 99
    starters_idx = out[out["Decision"] == "START"].sort_values(by=score_col, ascending=False).index

    pos_counts = {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "K": 0, "DEF": 0}
    pos_limits = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
    slot_rank_map = {"QB": 1, "RB1": 2, "RB2": 3, "WR1": 4, "WR2": 5, "TE": 6, "FLEX": 7, "K": 8, "DEF": 9}

    for idx in starters_idx:
        p = out.loc[idx, "Pos"]
        if pos_counts.get(p, 0) < pos_limits.get(p, 1):
            pos_counts[p] = pos_counts.get(p, 0) + 1
            slot_name = f"{p}{pos_counts[p]}" if p in {"RB", "WR"} else p
        else:
            slot_name = "FLEX"
        out.loc[idx, "Slot"] = slot_name
        out.loc[idx, "Slot_Order"] = slot_rank_map.get(slot_name, 90)

    bench_idx = out[out["Decision"] == "SIT"].index
    for idx in bench_idx:
        p = out.loc[idx, "Pos"]
        out.loc[idx, "Slot"] = f"BN ({p})"
        out.loc[idx, "Slot_Order"] = 100 + CBS_POS_ORDER.get(p, 9)

    return out.sort_values(by=["Slot_Order", score_col], ascending=[True, False])


# ==============================================================================
# 6. MAIN HEADER & 7-TAB INTERFACE
# ==============================================================================
if ready_icon and ready_icon.exists():
    h1, h2 = st.columns([1, 8])
    with h1:
        st.image(str(ready_icon), width=76)
    with h2:
        st.title("WHISKEY POWER — FEFFL 14-Team Optimizer")
        st.caption(f"Week {current_week} | {strategy} | FAAB: ${faab_rem}/${faab_start} | Playoff SOS Weight: {int(playoff_weight*100)}% | 1 Keeper (Rd 8+)")
else:
    st.title("🥃 WHISKEY POWER — FEFFL 14-Team Optimizer")

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "📊 1. Start/Sit MILP",
    "🔥 2. Waiver Radar",
    "💵 3. FAAB Calculator",
    "⚖️ 4. 2-for-1 Playoff Trade",
    "🛡️ 5. DST/K 2-Wk Streamer",
    "🔒 6. Rd 8+ Keeper Vault",
    "🧠 7. AI Coach Brief"
])

my_roster = df[df["Status"] == "My Roster"].copy()
waivers_all = df[df["Status"] == "Waiver"].copy()
opt_df = optimize_lineup(my_roster, "Opt_Score")
starters = opt_df[opt_df["Decision"] == "START"].sort_values(by="Slot_Order")
bench = opt_df[opt_df["Decision"] == "SIT"].sort_values(by=["Slot_Order", "Opt_Score"], ascending=[True, False])

with tab1:
    c_start, c_bench = st.columns([3, 2])
    with c_start:
        st.markdown("#### 🟢 Starting Lineup (QB, 2 RB, 2 WR, TE, FLEX, K, DEF)")
        s_cols = [c for c in ["Slot", "Pos", "Player", "Team", "Opp", "DvP_Rank", "DvP_Tier", "Proj_Rec", "Proj_Pts", "Floor", "Ceiling", "Opt_Score"] if c in starters.columns]
        st.dataframe(starters[s_cols], use_container_width=True, hide_index=True)
        m1, m2, m3 = st.columns(3)
        m1.metric("Projected PPR Total", f"{starters['Proj_Pts'].sum():.1f} pts")
        m2.metric("Floor (10th Pct)", f"{starters['Floor'].sum():.1f} pts")
        m3.metric("Ceiling (90th Pct)", f"{starters['Ceiling'].sum():.1f} pts")
    with c_bench:
        st.markdown("#### 🔴 Recommended Bench (Sit)")
        b_cols = [c for c in ["Pos", "Player", "Opp", "DvP_Tier", "Proj_Rec", "Proj_Pts", "Floor", "Ceiling", "Opt_Score"] if c in bench.columns]
        st.dataframe(bench[b_cols], use_container_width=True, hide_index=True)

with tab2:
    f1, f2, f3 = st.columns(3)
    pos_f = f1.multiselect("Positions:", ["QB", "RB", "WR", "TE"], default=["RB", "WR", "TE"])
    min_snap = f2.slider("Min 2-Week Snap % Surge:", -20.0, 30.0, 0.0, 2.5)
    min_wopr = f3.slider("Min WOPR:", 0.0, 0.70, 0.15, 0.05)
    w_pool = waivers_all[(waivers_all["Pos"].isin(pos_f)) & (waivers_all["Snap_Trend_2W"] >= min_snap) & ((waivers_all["WOPR"] >= min_wopr) | (waivers_all["Pos"].isin(["RB", "QB"])))].sort_values(by="Breakout_Index", ascending=False)
    w_cols = [c for c in ["Pos", "Player", "Team", "Opp", "Snap_%", "Snap_Trend_2W", "Target_%", "WOPR", "ROS_PPG", "Playoff_Tier", "Keeper_Tag", "Breakout_Index", "VORP_14T"] if c in w_pool.columns]
    st.dataframe(w_pool[w_cols], use_container_width=True, hide_index=True)

with tab3:
    cl, cr = st.columns(2)
    skill_w = waivers_all[waivers_all["Pos"].isin(["QB", "RB", "WR", "TE"])]["Player"].tolist() or waivers_all["Player"].tolist()
    with cl:
        target_w = st.selectbox("Select Waiver Target:", skill_w) if skill_w else "None"
        w_row = waivers_all[waivers_all["Player"] == target_w].iloc[0] if skill_w else df.iloc[0]
        def_idx = 0 if w_row["VORP_14T"] >= 5.5 else (1 if w_row["VORP_14T"] >= 3.2 else 2)
        t_choice = st.selectbox("Usage Tier:", ["League Winner / Bellcow Takeover (Base 60%)", "Every-Week PPR Starter / High Target Slot (Base 30%)", "Flex Depth / High-Ceiling Handcuff (Base 12%)", "1-Week Injury or Bye Streamer (Base 6%)"], index=def_idx)
        n_choice = st.select_slider("Positional Need:", options=["Luxury Stash (0.5x)", "Bench Depth (0.75x)", "Flex Upgrade (1.0x)", "Desperate Starter Hole (1.35x)"], value="Flex Upgrade (1.0x)")
        drop_list = bench["Player"].tolist() if not bench.empty else my_roster["Player"].tolist()
        drop_cand = st.selectbox("Drop Candidate:", drop_list) if drop_list else "None"

    t_pct = {"League Winner / Bellcow Takeover (Base 60%)": 0.60, "Every-Week PPR Starter / High Target Slot (Base 30%)": 0.30, "Flex Depth / High-Ceiling Handcuff (Base 12%)": 0.12, "1-Week Injury or Bye Streamer (Base 6%)": 0.06}[t_choice]
    n_mult = {"Luxury Stash (0.5x)": 0.50, "Bench Depth (0.75x)": 0.75, "Flex Upgrade (1.0x)": 1.00, "Desperate Starter Hole (1.35x)": 1.35}[n_choice]
    urg = 1.0 + 0.40 * (current_week / 17.0)
    lev = 1.08 if faab_rem > opp_avg_faab else 0.95
    best_k_surp = my_roster["Keeper_Surplus"].max() if not my_roster.empty else 0.0
    k_boost = 1.20 if (w_row["Keeper_Eligible"] and w_row["Keeper_Surplus"] >= max(20.0, best_k_surp * 0.75)) else 1.00

    rec_bid = int(round(faab_rem * t_pct * n_mult * urg * lev * k_boost))
    if rec_bid % 5 == 0 and rec_bid > 0:
        rec_bid += 1
    elif (rec_bid - 1) % 5 == 0 and rec_bid > 5:
        rec_bid += 1
    rec_bid = min(faab_rem, max(1, rec_bid))
    cons_bid = max(1, int(round(rec_bid * 0.72)))
    aggr_bid = min(faab_rem, int(round(rec_bid * 1.28)) + 1)

    with cr:
        st.markdown(f"### Recommended Bid: **{target_w}**")
        b1, b2, b3 = st.columns(3)
        b1.metric("Conservative", f"${cons_bid}")
        b2.metric("🎯 Optimal Psych Bid", f"${rec_bid}")
        b3.metric("Aggressive Lock", f"${aggr_bid}")
        st.markdown(f"* **Usage:** `{w_row['Snap_%']}% Snaps ({w_row['Snap_Trend_2W']:+.1f}% 2W)` | `WOPR: {w_row['WOPR']}`\n* **Keeper Status:** `{w_row['Keeper_Tag']}` (`+{w_row['Keeper_Surplus']} Surplus`)")

with tab4:
    tc1, tc2 = st.columns(2)
    my_sk = my_roster[my_roster["Pos"].isin(["QB", "RB", "WR", "TE"])]["Player"].tolist()
    opp_pool = df[df["Status"] == "Opponent Roster"].copy()
    opp_sk = opp_pool["Player"].tolist()
    with tc1:
        send_p = st.multiselect("📤 You Send:", my_sk, default=my_sk[:2] if len(my_sk) >= 2 else my_sk)
    with tc2:
        recv_p = st.multiselect("📥 You Receive:", opp_sk, default=opp_sk[:1] if opp_sk else [])

    reg_delta, po_delta = 0.0, 0.0
    if send_p and recv_p:
        s_df = my_roster[my_roster["Player"].isin(send_p)]
        r_df = opp_pool[opp_pool["Player"].isin(recv_p)]
        s_diff = len(send_p) - len(recv_p)
        post_r = pd.concat([my_roster[~my_roster["Player"].isin(send_p)], r_df], ignore_index=True)
        add_v, drop_v = 0.0, 0.0
        sk_w = waivers_all[waivers_all["Pos"].isin(["RB", "WR", "TE"])].sort_values(by="Trade_Value", ascending=False)
        if s_diff > 0 and not sk_w.empty:
            w_adds = sk_w.head(s_diff)
            post_r = pd.concat([post_r, w_adds], ignore_index=True)
            add_v = w_adds["Trade_Value"].sum()
        elif s_diff < 0:
            d_df = post_r[post_r["Pos"].isin(["RB", "WR", "TE"]) & (~post_r["Player"].isin(recv_p))].sort_values(by="Trade_Value").head(abs(s_diff))
            drop_v = d_df["Trade_Value"].sum()
            post_r = post_r[~post_r["Player"].isin(d_df["Player"])]

        reg_delta = optimize_lineup(post_r, "ROS_PPG").query("Decision == 'START'")["ROS_PPG"].sum() - optimize_lineup(my_roster, "ROS_PPG").query("Decision == 'START'")["ROS_PPG"].sum()
        po_delta = optimize_lineup(post_r, "Playoff_PPG").query("Decision == 'START'")["Playoff_PPG"].sum() - optimize_lineup(my_roster, "Playoff_PPG").query("Decision == 'START'")["Playoff_PPG"].sum()

        st.dataframe(pd.concat([s_df.assign(Side="📤 Send"), r_df.assign(Side="📥 Receive")])[["Side", "Player", "Pos", "ROS_PPG", "Playoff_Slate", "Playoff_PPG", "Playoff_Tier", "Keeper_Tag", "Trade_Value"]], use_container_width=True, hide_index=True)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Send Value", f"{s_df['Trade_Value'].sum():.1f}")
        k2.metric("Effective Get Value", f"{r_df['Trade_Value'].sum() + add_v - drop_v:.1f}")
        k3.metric("Reg Season Starting 9 Δ", f"{reg_delta:+.2f} PPG")
        k4.metric("🏆 Playoff Starting 9 Δ", f"{po_delta:+.2f} PPG")

with tab5:
    d_mode = st.radio("DST Lens:", ["🔥 Best 2-Week Smash Holds", "💎 $0 FAAB Next-Week Pre-Stashes", "⚡ Best Week N Streamer"], horizontal=True)
    d_pool = df[(df["Pos"] == "DEF") & (df["Status"].isin(["My Roster", "Waiver"]))].copy()
    sort_c = "Two_Wk_Proj_Pts" if "2-Week" in d_mode else ("WkN1_Proj_Pts" if "$0 FAAB" in d_mode else "Proj_Pts")
    st.dataframe(d_pool.sort_values(by=sort_c, ascending=False)[["Player", "Status", "Stash_Tag", "Opp", "Opp_Vegas_Implied", "Opp_Sack_Rate_Pct", "Proj_Pts", "WkN1_Opp", "WkN1_Opp_Implied", "WkN1_Proj_Pts", "Two_Wk_Proj_Pts"]], use_container_width=True, hide_index=True)

    st.markdown("#### 🎯 Top Kicker Streamers")
    k_pool = df[(df["Pos"] == "K") & (df["Status"].isin(["My Roster", "Waiver"]))].sort_values(by="Proj_Pts", ascending=False)
    st.dataframe(k_pool[["Player", "Team", "Status", "Opp", "Vegas_Implied", "Roof", "Proj_FGs", "Proj_Pts", "DvP_Tier"]], use_container_width=True, hide_index=True)

with tab6:
    my_k = my_roster[my_roster["Keeper_Eligible"]].sort_values(by="Keeper_Surplus", ascending=False)
    st.dataframe(my_k[["Player", "Pos", "Team", "Draft_Round", "Keeper_Cost_Rd", "ROS_PPG", "VORP_14T", "Proj_Next_Yr_Rd", "Keeper_Surplus", "Keeper_Tag"]], use_container_width=True, hide_index=True)

with tab7:
    prompt_text = f"""You are an elite 14-Team Full-PPR Fantasy Football Analyst for Whiskey Power (FEFFL).
Week {current_week} | Mode: {strategy} | FAAB: ${faab_rem}/${faab_start}
Starters: {starters[['Slot', 'Pos', 'Player', 'Opp', 'Proj_Pts', 'Floor', 'Ceiling']].to_dict(orient='records')}
Bench: {bench[['Pos', 'Player', 'Opp', 'Proj_Pts', 'ROS_PPG', 'Playoff_PPG']].to_dict(orient='records')}
Waiver Target: {target_w} (Bid: ${rec_bid}, Drop: {drop_cand})
Trade Eval: Send {send_p} for {recv_p} (Reg Δ: {reg_delta:+.2f} PPG, Playoff Δ: {po_delta:+.2f} PPG)"""
    st.code(prompt_text, language="markdown")
