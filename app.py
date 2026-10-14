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

# Optional live dependencies (graceful fallback if still installing)
try:
    import nfl_data_py as nfl
except ImportError:
    nfl = None

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sync_playwright = None


# ==============================================================================
# 0. AUTOMATIC WHISKEY POWER ICON CROPPER & MOBILE PWA INJECTOR
# ==============================================================================
ICON_PATH = Path("icon.png")
SECRETS_PATH = Path(".streamlit/secrets.toml")
PROFILE_DIR = Path(".cbs_browser_profile")


def prepare_whiskey_power_icon() -> Optional[Path]:
    """
    Finds any uploaded logo image in the repo, crops out the top timestamp
    notification bar, and saves a clean 512x512 square icon.png for iPad/iPhone.
    """
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
        # If portrait screenshot with top timestamp, crop tightly around the emblem
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
    """Injects Apple Touch Icon & Full-Screen Standalone PWA headers for iPad/iPhone."""
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

            const metas = [
                {{name: 'apple-mobile-web-app-capable', content: 'yes'}},
                {{name: 'mobile-web-app-capable', content: 'yes'}},
                {{name: 'apple-mobile-web-app-status-bar-style', content: 'black-translucent'}},
                {{name: 'apple-mobile-web-app-title', content: '{app_name}'}}
            ];
            metas.forEach(m => {{
                let metaTag = window.parent.document.createElement('meta');
                metaTag.name = m.name;
                metaTag.content = m.content;
                head.appendChild(metaTag);
            }});
        }})();
        </script>
        """,
        height=0,
    )


inject_mobile_app_icon()


# ==============================================================================
# 1. SECRETS & PLAYWRIGHT AUTO-LOGIN MODULE
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


def update_secrets_toml(updates: Dict[str, str], path: Path = SECRETS_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        updated_keys = set()
        new_lines = []
        for line in lines:
            match = re.match(r'^([A-Za-z0-9_]+)\s*=\s*.*$', line.strip())
            if match and match.group(1) in updates:
                k = match.group(1)
                v = str(updates[k]).replace('"', '\\"')
                new_lines.append(f'{k} = "{v}"')
                updated_keys.add(k)
            else:
                new_lines.append(line)
        for k, v in updates.items():
            if k not in updated_keys and v is not None:
                safe_v = str(v).replace('"', '\\"')
                new_lines.append(f'{k} = "{safe_v}"')
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def refresh_cbs_session(
    email: Optional[str] = None,
    password: Optional[str] = None,
    league_id: Optional[str] = None,
    headless: bool = True,
) -> Dict[str, str]:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Paste your CBS_SESS cookie directly or check packages.txt.")

    email = email or get_config_secret("CBS_EMAIL")
    password = password or get_config_secret("CBS_PASSWORD")
    league_id = league_id or get_config_secret("CBS_LEAGUE_ID", "my-14team-ppr")

    if not email or not password:
        raise ValueError("Add CBS_EMAIL and CBS_PASSWORD in Streamlit Secrets (or paste CBS_SESS cookie directly).")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            headless=headless,
            viewport={"width": 1280, "height": 840},
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        try:
            page.goto("https://www.cbssports.com/login", wait_until="domcontentloaded", timeout=25000)
            time.sleep(1.5)

            cbs_sess = None
            for c in context.cookies():
                if c.get("name") == "CBS_SESS" and "cbssports.com" in c.get("domain", ""):
                    cbs_sess = c.get("value", "").strip()

            if not cbs_sess or "/login" in page.url:
                for sel in ['input[name="userid"]', 'input[name="email"]', 'input[type="email"]']:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.fill(email)
                        break
                for sel in ['input[name="password"]', 'input[type="password"]']:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.fill(password)
                        break
                page.keyboard.press("Enter")

                for _ in range(15):
                    for c in context.cookies():
                        if c.get("name") == "CBS_SESS" and "cbssports.com" in c.get("domain", ""):
                            cbs_sess = c.get("value", "").strip()
                    if cbs_sess:
                        break
                    time.sleep(1.0)

            if not cbs_sess:
                raise RuntimeError("CBS login required CAPTCHA or failed. Paste CBS_SESS cookie directly in the sidebar.")

            res = {
                "CBS_SESS": cbs_sess,
                "CBS_LEAGUE_ID": league_id,
                "CBS_LAST_REFRESH": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            update_secrets_toml(res)
            return res
        finally:
            context.close()


# ==============================================================================
# 2. NFL_DATA_PY USAGE, DVP, 2-WK DST & PLAYOFF SOS ENGINE
# ==============================================================================
class NFLUsageEngine:
    TEAM_MAP = {"LA": "LAR", "WSH": "WAS", "JAC": "JAX"}
    TEAM_MASCOTS = {
        "ARI": "Cardinals DST", "ATL": "Falcons DST", "BAL": "Ravens DST", "BUF": "Bills DST",
        "CAR": "Panthers DST", "CHI": "Bears DST", "CIN": "Bengals DST", "CLE": "Browns DST",
        "DAL": "Cowboys DST", "DEN": "Broncos DST", "DET": "Lions DST", "GB": "Packers DST",
        "HOU": "Texans DST", "IND": "Colts DST", "JAX": "Jaguars DST", "KC": "Chiefs DST",
        "LAC": "Chargers DST", "LAR": "Rams DST", "LV": "Raiders DST", "MIA": "Dolphins DST",
        "MIN": "Vikings DST", "NE": "Patriots DST", "NO": "Saints DST", "NYG": "Giants DST",
        "NYJ": "Jets DST", "PHI": "Eagles DST", "PIT": "Steelers DST", "SEA": "Seahawks DST",
        "SF": "49ers DST", "TB": "Buccaneers DST", "TEN": "Titans DST", "WAS": "Commanders DST"
    }

    def __init__(self, season: int = None):
        self.season = season or datetime.now().year

    @staticmethod
    def normalize_name(name: str) -> str:
        if not isinstance(name, str):
            return ""
        clean = re.sub(r"\b(Jr\.?|Sr\.?|III|II|IV|V)\b", "", name, flags=re.IGNORECASE)
        clean = re.sub(r"[^a-zA-Z\s]", "", clean)
        return " ".join(clean.split()).strip()

    def _standardize_team(self, s: pd.Series) -> pd.Series:
        return s.replace(self.TEAM_MAP)

    def fetch_upcoming_vegas_lines(self, target_week: int) -> pd.DataFrame:
        sched = nfl.import_schedules([self.season])
        sched = sched[sched["week"] == target_week].copy()
        if sched.empty:
            return pd.DataFrame(columns=[
                "Team", "Opp", "Opp_Clean", "Vegas_Implied",
                "Opp_Vegas_Implied", "Team_Spread", "Roof", "Is_Dome"
            ])
        sched["home_team"] = self._standardize_team(sched["home_team"])
        sched["away_team"] = self._standardize_team(sched["away_team"])
        total = sched["total_line"].fillna(44.0)
        spread = sched["spread_line"].fillna(0.0)
        roof = sched["roof"].fillna("outdoors").str.lower() if "roof" in sched.columns else pd.Series("outdoors", index=sched.index)

        h_imp = ((total / 2.0) + (spread / 2.0)).round(1)
        a_imp = ((total / 2.0) - (spread / 2.0)).round(1)

        home_df = pd.DataFrame({
            "Team": sched["home_team"], "Opp": sched["away_team"], "Opp_Clean": sched["away_team"],
            "Vegas_Implied": h_imp, "Opp_Vegas_Implied": a_imp, "Team_Spread": -spread,
            "Roof": roof, "Is_Dome": roof.isin(["dome", "closed"])
        })
        away_df = pd.DataFrame({
            "Team": sched["away_team"], "Opp": "@" + sched["home_team"], "Opp_Clean": sched["home_team"],
            "Vegas_Implied": a_imp, "Opp_Vegas_Implied": h_imp, "Team_Spread": spread,
            "Roof": roof, "Is_Dome": roof.isin(["dome", "closed"])
        })
        return pd.concat([home_df, away_df], ignore_index=True)

    def calculate_dvp_multipliers(self, weekly_df: pd.DataFrame) -> pd.DataFrame:
        df = weekly_df.copy()
        df["opponent_team"] = self._standardize_team(df["opponent_team"])
        df = df.dropna(subset=["opponent_team", "position", "fantasy_points_ppr"])

        room_totals = (
            df.groupby(["opponent_team", "position", "week"], as_index=False)["fantasy_points_ppr"]
            .sum().rename(columns={"fantasy_points_ppr": "pos_pts_allowed"})
        )
        max_wk = int(room_totals["week"].max()) if not room_totals.empty else 1

        def agg_def(g: pd.DataFrame) -> pd.Series:
            s_avg = g["pos_pts_allowed"].mean()
            r_avg = g[g["week"] >= max(1, max_wk - 3)]["pos_pts_allowed"].mean()
            blended = (0.60 * (r_avg if not pd.isna(r_avg) else s_avg)) + (0.40 * s_avg)
            return pd.Series({"Def_Games": len(g), "DvP_Pts_Allowed": round(blended, 2)})

        dvp = room_totals.groupby(["opponent_team", "position"], as_index=False).apply(agg_def)
        dvp["Pos_League_Avg"] = dvp.groupby("position")["DvP_Pts_Allowed"].transform("mean").round(2)
        raw_ratio = dvp["DvP_Pts_Allowed"] / dvp["Pos_League_Avg"].replace(0, 1)
        shrunk_ratio = ((dvp["Def_Games"] * raw_ratio) + 5.0) / (dvp["Def_Games"] + 5.0)

        dvp["DvP_Mult"] = np.clip(1.0 + 0.55 * (shrunk_ratio - 1.0), 0.82, 1.18).round(3)
        dvp["DvP_Diff_Pct"] = ((shrunk_ratio - 1.0) * 100.0).round(1)
        dvp["DvP_Rank"] = dvp.groupby("position")["DvP_Pts_Allowed"].rank(ascending=True, method="min").astype(int)
        dvp["DvP_Tier"] = np.where(
            dvp["DvP_Rank"] >= 25, "🟢 Smash",
            np.where(dvp["DvP_Rank"] <= 8, "🔴 Tough", "🟡 Neutral")
        )
        return dvp.rename(columns={"opponent_team": "Opp_Clean", "position": "Pos"})[
            ["Opp_Clean", "Pos", "DvP_Pts_Allowed", "DvP_Diff_Pct", "DvP_Rank", "DvP_Mult", "DvP_Tier"]
        ]

    def calculate_playoff_sos(self, dvp_df: pd.DataFrame) -> pd.DataFrame:
        sched = nfl.import_schedules([self.season])
        po_sched = sched[sched["week"].isin([15, 16, 17])].copy()
        if po_sched.empty:
            return pd.DataFrame(columns=[
                "Team", "Pos", "Playoff_Slate", "Wk15_Mult", "Wk16_Mult", "Wk17_Mult",
                "Playoff_Mult", "Playoff_Boost_Pct", "Playoff_SOS_Rank", "Playoff_Tier"
            ])
        po_sched["home_team"] = self._standardize_team(po_sched["home_team"])
        po_sched["away_team"] = self._standardize_team(po_sched["away_team"])

        h_g = pd.DataFrame({"week": po_sched["week"], "Team": po_sched["home_team"], "Opp_Display": po_sched["away_team"], "Opp_Clean": po_sched["away_team"]})
        a_g = pd.DataFrame({"week": po_sched["week"], "Team": po_sched["away_team"], "Opp_Display": "@" + po_sched["home_team"], "Opp_Clean": po_sched["home_team"]})
        po_games = pd.concat([h_g, a_g], ignore_index=True)

        po_pos = po_games.merge(pd.DataFrame({"Pos": ["QB", "RB", "WR", "TE"]}), how="cross")
        po_pos = pd.merge(po_pos, dvp_df[["Opp_Clean", "Pos", "DvP_Mult"]], on=["Opp_Clean", "Pos"], how="left")
        po_pos["DvP_Mult"] = po_pos["DvP_Mult"].fillna(1.00)

        opp_piv = po_pos.pivot(index=["Team", "Pos"], columns="week", values="Opp_Display").reset_index().rename(columns={15: "Wk15_Opp", 16: "Wk16_Opp", 17: "Wk17_Opp"})
        mult_piv = po_pos.pivot(index=["Team", "Pos"], columns="week", values="DvP_Mult").reset_index().rename(columns={15: "Wk15_Mult", 16: "Wk16_Mult", 17: "Wk17_Mult"})
        po = pd.merge(opp_piv, mult_piv, on=["Team", "Pos"], how="left")

        for c in ["Wk15_Opp", "Wk16_Opp", "Wk17_Opp"]:
            po[c] = po.get(c, pd.Series("BYE", index=po.index)).fillna("BYE")
        for c in ["Wk15_Mult", "Wk16_Mult", "Wk17_Mult"]:
            po[c] = po.get(c, pd.Series(1.00, index=po.index)).fillna(1.00).round(3)

        po["Playoff_Slate"] = po["Wk15_Opp"] + " → " + po["Wk16_Opp"] + " → " + po["Wk17_Opp"]
        po["Playoff_Mult"] = (0.40 * po["Wk15_Mult"] + 0.35 * po["Wk16_Mult"] + 0.25 * po["Wk17_Mult"]).round(3)
        po["Playoff_Boost_Pct"] = ((po["Playoff_Mult"] - 1.0) * 100.0).round(1)
        po["Playoff_SOS_Rank"] = po.groupby("Pos")["Playoff_Mult"].rank(ascending=True, method="min").astype(int)
        po["Playoff_Tier"] = np.where(
            po["Playoff_SOS_Rank"] >= 25, "🏆 League-Winner Slate (Top-8)",
            np.where(po["Playoff_SOS_Rank"] <= 8, "❄️ Playoff Landmine (Bottom-8)", "⚖️ Neutral Slate")
        )
        return po

    def fetch_snap_trends(self) -> pd.DataFrame:
        snaps = nfl.import_snap_counts([self.season])
        snaps = snaps[snaps["game_type"] == "REG"].copy()
        if snaps.empty:
            return pd.DataFrame(columns=["Clean_Name", "Pos", "Snap_%", "Snap_Trend_2W"])
        snaps["Clean_Name"] = snaps["player"].apply(self.normalize_name)
        snaps["Pos"] = snaps["position"].replace({"HB": "RB", "FB": "RB"})
        snaps["offense_pct"] = (snaps["offense_pct"] * 100.0).round(1)
        max_wk = int(snaps["week"].max())

        def calc_snap(g: pd.DataFrame) -> pd.Series:
            rec = g[g["week"] >= max(1, max_wk - 1)]["offense_pct"].mean()
            ear = g[g["week"] < max(1, max_wk - 1)]["offense_pct"].mean()
            curr = rec if not pd.isna(rec) else g["offense_pct"].mean()
            base = ear if not pd.isna(ear) else curr
            return pd.Series({"Snap_%": int(round(curr)), "Snap_Trend_2W": round(curr - base, 1)})

        return snaps.groupby(["Clean_Name", "Pos"], as_index=False).apply(calc_snap)

    def build_dst_streaming_dataframe(self, weekly_df: pd.DataFrame, vegas_n: pd.DataFrame, vegas_n1: pd.DataFrame) -> pd.DataFrame:
        df = weekly_df.copy()
        df["recent_team"] = self._standardize_team(df["recent_team"])
        df["opponent_team"] = self._standardize_team(df["opponent_team"])
        df["sacks_taken"] = df["sacks"].fillna(0.0) if "sacks" in df.columns else 0.0
        df["ints_thrown"] = df["interceptions"].fillna(0.0) if "interceptions" in df.columns else 0.0
        f_cols = [c for c in ["sack_fumbles_lost", "rushing_fumbles_lost", "receiving_fumbles_lost"] if c in df.columns]
        df["turnovers"] = df["ints_thrown"] + (df[f_cols].fillna(0.0).sum(axis=1) if f_cols else 0.0)
        df["pass_att"] = df["attempts"].fillna(0.0) if "attempts" in df.columns else 32.0

        off_wk = df.groupby(["recent_team", "week"], as_index=False).agg(sacks_allowed=("sacks_taken", "sum"), turnovers_given=("turnovers", "sum"), pass_attempts=("pass_att", "sum"))
        off_wk["sack_rate_pct"] = (off_wk["sacks_allowed"] / np.maximum(15.0, off_wk["pass_attempts"] + off_wk["sacks_allowed"])) * 100.0
        off_sum = off_wk.groupby("recent_team", as_index=False).agg(Opp_Sacks_Gm=("sacks_allowed", "mean"), Opp_Sack_Rate_Pct=("sack_rate_pct", "mean"), Opp_TO_Gm=("turnovers_given", "mean")).rename(columns={"recent_team": "Opp_Clean"})

        def_wk = df.groupby(["opponent_team", "week"], as_index=False).agg(def_sacks=("sacks_taken", "sum"), def_turnovers=("turnovers", "sum"), opp_pass_att=("pass_att", "sum"))
        def_wk["def_sack_rate_pct"] = (def_wk["def_sacks"] / np.maximum(15.0, def_wk["opp_pass_att"] + def_wk["def_sacks"])) * 100.0
        def_sum = def_wk.groupby("opponent_team", as_index=False).agg(Def_Sacks_Gm=("def_sacks", "mean"), Def_Sack_Rate_Pct=("def_sack_rate_pct", "mean"), Def_TO_Gm=("def_turnovers", "mean")).rename(columns={"opponent_team": "Team"})

        dst = pd.DataFrame({"Team": list(self.TEAM_MASCOTS.keys()), "Player": list(self.TEAM_MASCOTS.values()), "Pos": "DEF"})
        dst["Clean_Name"] = dst["Player"].apply(self.normalize_name)
        dst = dst.merge(vegas_n, on="Team", how="left").merge(def_sum, on="Team", how="left").merge(off_sum, on="Opp_Clean", how="left")

        dst["Opp"] = dst["Opp"].fillna("BYE")
        dst["Opp_Vegas_Implied"] = dst["Opp_Vegas_Implied"].fillna(22.0)
        dst["Vegas_Implied"] = dst["Opp_Vegas_Implied"]
        for c, d in [("Opp_Sacks_Gm", 2.6), ("Opp_Sack_Rate_Pct", 7.2), ("Opp_TO_Gm", 1.3), ("Def_Sacks_Gm", 2.6), ("Def_Sack_Rate_Pct", 7.2), ("Def_TO_Gm", 1.3)]:
            dst[c] = dst[c].fillna(d).round(2)

        dst["Proj_Sacks"] = (0.60 * dst["Opp_Sacks_Gm"] + 0.40 * dst["Def_Sacks_Gm"]).round(2)
        dst["Proj_TO"] = (0.60 * dst["Opp_TO_Gm"] + 0.40 * dst["Def_TO_Gm"]).round(2)
        pa_n = np.clip(10.5 - 0.42 * dst["Opp_Vegas_Implied"], -2.0, 10.0)
        dst["Proj_Pts"] = np.where(dst["Opp"] == "BYE", 0.0, pa_n + dst["Proj_Sacks"] + 3.08 * dst["Proj_TO"]).round(1)

        wk_n1 = vegas_n1[["Team", "Opp", "Opp_Clean", "Opp_Vegas_Implied"]].rename(columns={"Opp": "WkN1_Opp", "Opp_Clean": "WkN1_Opp_Clean", "Opp_Vegas_Implied": "WkN1_Vegas_Raw"})
        off_n1 = off_sum.rename(columns={"Opp_Clean": "WkN1_Opp_Clean", "Opp_Sacks_Gm": "WkN1_Opp_Sacks_Gm", "Opp_Sack_Rate_Pct": "WkN1_Opp_Sack_Rate_Pct", "Opp_TO_Gm": "WkN1_Opp_TO_Gm"})
        dst = dst.merge(wk_n1, on="Team", how="left").merge(off_n1, on="WkN1_Opp_Clean", how="left")

        dst["WkN1_Opp"] = dst["WkN1_Opp"].fillna("BYE")
        dst["WkN1_Opp_Sacks_Gm"] = dst["WkN1_Opp_Sacks_Gm"].fillna(2.6).round(2)
        dst["WkN1_Opp_Sack_Rate_Pct"] = dst["WkN1_Opp_Sack_Rate_Pct"].fillna(7.2).round(1)
        dst["WkN1_Opp_TO_Gm"] = dst["WkN1_Opp_TO_Gm"].fillna(1.3).round(2)

        synth_n1 = np.clip(22.5 - 1.35 * (dst["WkN1_Opp_Sacks_Gm"] - 2.6) - 2.1 * (dst["WkN1_Opp_TO_Gm"] - 1.3), 14.5, 29.0)
        dst["WkN1_Opp_Implied"] = np.where(dst["WkN1_Vegas_Raw"].isna() | (dst["WkN1_Vegas_Raw"] == 22.0), synth_n1, dst["WkN1_Vegas_Raw"]).round(1)
        n1_sacks = 0.60 * dst["WkN1_Opp_Sacks_Gm"] + 0.40 * dst["Def_Sacks_Gm"]
        n1_to = 0.60 * dst["WkN1_Opp_TO_Gm"] + 0.40 * dst["Def_TO_Gm"]
        pa_n1 = np.clip(10.5 - 0.42 * dst["WkN1_Opp_Implied"], -2.0, 10.0)
        dst["WkN1_Proj_Pts"] = np.where(dst["WkN1_Opp"] == "BYE", 0.0, pa_n1 + n1_sacks + 3.08 * n1_to).round(1)
        dst["Two_Wk_Proj_Pts"] = (dst["Proj_Pts"] + dst["WkN1_Proj_Pts"]).round(1)

        dst["Stash_Tag"] = np.select(
            [(dst["Proj_Pts"] >= 8.5) & (dst["WkN1_Proj_Pts"] >= 8.5), (dst["Proj_Pts"] < 8.5) & (dst["WkN1_Proj_Pts"] >= 9.0), (dst["Proj_Pts"] >= 8.8) & (dst["WkN1_Proj_Pts"] < 8.5)],
            ["🔥 2-Week Smash Hold", "💎 $0 FAAB Pre-Stash (Wk N+1)", "⚡ 1-Week Stream & Drop"],
            default="🛑 Avoid / Landmine"
        )
        dst["ROS_PPG"] = (4.2 + dst["Def_Sacks_Gm"] + 2.4 * dst["Def_TO_Gm"]).round(1)
        dst["Floor"] = np.where(dst["Opp"] == "BYE", 0.0, np.maximum(1.0, dst["Proj_Pts"] - 4.2)).round(1)
        dst["Ceiling"] = np.where(dst["Opp"] == "BYE", 0.0, dst["Proj_Pts"] + 6.8).round(1)
        dst["Proj_Rec"] = 0.0
        dst["Snap_%"] = 100
        dst["Snap_Trend_2W"] = 0.0
        dst["Target_%"] = dst["Opp_Sack_Rate_Pct"]
        dst["Air_Yards_%"] = 0.0
        dst["WOPR"] = 0.0
        dst["DvP_Rank"] = 16
        dst["DvP_Tier"] = dst["Stash_Tag"]
        dst["Breakout_Index"] = (dst["Proj_Pts"] * 5.5 + dst["WkN1_Proj_Pts"] * 4.5).round(1)
        return dst

    def build_kicker_streaming_dataframe(self, vegas_df: pd.DataFrame, dst_df: pd.DataFrame) -> pd.DataFrame:
        rosters = nfl.import_seasonal_rosters([self.season])
        kickers = rosters[rosters["position"] == "K"].copy()
        if kickers.empty:
            return pd.DataFrame()
        name_col = "player_name" if "player_name" in kickers.columns else "full_name"
        kickers["Team"] = self._standardize_team(kickers["team"])
        kickers = kickers.drop_duplicates(subset=["Team"], keep="last")

        k_df = pd.DataFrame({"Player": kickers[name_col].values, "Clean_Name": kickers[name_col].apply(self.normalize_name).values, "Pos": "K", "Team": kickers["Team"].values})
        k_df = k_df.merge(vegas_df, on="Team", how="left").merge(dst_df[["Team", "Def_Sack_Rate_Pct"]].rename(columns={"Team": "Opp_Clean"}), on="Opp_Clean", how="left")

        k_df["Opp"] = k_df["Opp"].fillna("BYE")
        k_df["Vegas_Implied"] = k_df["Vegas_Implied"].fillna(21.5)
        k_df["Team_Spread"] = k_df["Team_Spread"].fillna(0.0)
        k_df["Roof"] = k_df["Roof"].fillna("outdoors")
        k_df["Is_Dome"] = k_df["Is_Dome"].fillna(False)
        k_df["Def_Sack_Rate_Pct"] = k_df["Def_Sack_Rate_Pct"].fillna(7.0)

        exp_pat = (k_df["Vegas_Implied"] / 7.5) * 0.94
        script_fg = np.where(k_df["Team_Spread"] <= -1.5, 0.28, -0.15)
        stall_fg = np.clip((k_df["Def_Sack_Rate_Pct"] - 6.8) * 0.06, -0.15, 0.30)
        dome_b = np.where(k_df["Is_Dome"], 0.85, 0.0)

        k_df["Proj_FGs"] = np.where(k_df["Opp"] == "BYE", 0.0, np.maximum(0.8, 1.65 + (k_df["Vegas_Implied"] - 21.5) * 0.075 + script_fg + stall_fg)).round(2)
        k_df["Proj_Pts"] = np.where(k_df["Opp"] == "BYE", 0.0, k_df["Proj_FGs"] * 3.35 + exp_pat + dome_b).round(1)
        k_df["ROS_PPG"] = (8.2 + np.where(k_df["Is_Dome"], 0.5, 0.0)).round(1)
        k_df["Floor"] = np.where(k_df["Opp"] == "BYE", 0.0, np.maximum(2.0, k_df["Proj_Pts"] - 3.5)).round(1)
        k_df["Ceiling"] = np.where(k_df["Opp"] == "BYE", 0.0, k_df["Proj_Pts"] + 5.2).round(1)
        k_df["Proj_Rec"] = 0.0
        k_df["Snap_%"] = 100
        k_df["Snap_Trend_2W"] = 0.0
        k_df["Target_%"] = 0.0
        k_df["Air_Yards_%"] = 0.0
        k_df["WOPR"] = 0.0
        k_df["DvP_Rank"] = 16
        k_df["DvP_Tier"] = np.where(k_df["Vegas_Implied"] >= 23.5, "🟢 High Implied (23.5+)", "🟡 Neutral")
        k_df["Breakout_Index"] = (k_df["Proj_Pts"] * 5.0).round(1)
        return k_df

    def build_usage_dataframe(self, upcoming_week: int = None) -> pd.DataFrame:
        avail = set(nfl.see_weekly_cols())
        want = [
            "player_id", "player_display_name", "position", "recent_team", "opponent_team",
            "week", "season_type", "receptions", "targets", "receiving_air_yards",
            "target_share", "air_yards_share", "wopr", "sacks", "interceptions",
            "attempts", "fantasy_points_ppr"
        ]
        weekly = nfl.import_weekly_data([self.season], columns=[c for c in want if c in avail])
        if "season_type" in weekly.columns:
            weekly = weekly[weekly["season_type"] == "REG"].copy()
        weekly = weekly[weekly["position"].isin(["QB", "RB", "WR", "TE"])].copy()
        weekly["recent_team"] = self._standardize_team(weekly["recent_team"])

        max_week = int(weekly["week"].max()) if not weekly.empty else 1
        target_week = upcoming_week or (max_week + 1)

        for c in ["target_share", "air_yards_share", "wopr", "receiving_air_yards"]:
            weekly[c] = weekly[c].fillna(0.0) if c in weekly.columns else 0.0

        calc_wopr = 1.5 * weekly["target_share"] + 0.7 * weekly["air_yards_share"]
        weekly["wopr"] = np.where(weekly["wopr"] > 0, weekly["wopr"], calc_wopr)
        weekly = weekly.sort_values(["player_id", "week"])

        def agg_p(g: pd.DataFrame) -> pd.Series:
            l3 = g.tail(3)
            s_ppg = g["fantasy_points_ppr"].mean()
            r_ppg = l3["fantasy_points_ppr"].mean()
            blend = 0.65 * r_ppg + 0.35 * s_ppg
            std_v = g["fantasy_points_ppr"].std()
            if pd.isna(std_v) or std_v < 2.5:
                std_v = blend * 0.35
            return pd.Series({
                "Player": g["player_display_name"].iloc[-1],
                "Clean_Name": self.normalize_name(g["player_display_name"].iloc[-1]),
                "Pos": g["position"].iloc[-1],
                "Team": g["recent_team"].iloc[-1],
                "Proj_Rec": l3["receptions"].mean().round(1),
                "ROS_PPG": round(blend, 1),
                "Std_Dev_Actual": round(std_v, 2),
                "Target_%": (l3["target_share"].mean() * 100.0).round(1),
                "Air_Yards_%": (l3["air_yards_share"].mean() * 100.0).round(1),
                "WOPR": l3["wopr"].mean().round(2),
            })

        agg_df = weekly.groupby("player_id", as_index=False).apply(agg_p)
        snap_df = self.fetch_snap_trends()
        merged = pd.merge(agg_df, snap_df, on=["Clean_Name", "Pos"], how="left")
        merged["Snap_%"] = merged["Snap_%"].fillna(50).astype(int)
        merged["Snap_Trend_2W"] = merged["Snap_Trend_2W"].fillna(0.0).round(1)

        vegas_df = self.fetch_upcoming_vegas_lines(target_week)
        vegas_n1 = self.fetch_upcoming_vegas_lines(min(18, target_week + 1))
        merged = pd.merge(merged, vegas_df, on="Team", how="left")
        merged["Opp"] = merged["Opp"].fillna("BYE")
        merged["Opp_Clean"] = merged["Opp_Clean"].fillna("BYE")
        merged["Vegas_Implied"] = merged["Vegas_Implied"].fillna(21.5)

        dvp_df = self.calculate_dvp_multipliers(weekly)
        merged = pd.merge(merged, dvp_df, on=["Opp_Clean", "Pos"], how="left")
        merged["DvP_Rank"] = merged["DvP_Rank"].fillna(16).astype(int)
        merged["DvP_Diff_Pct"] = merged["DvP_Diff_Pct"].fillna(0.0).round(1)
        merged["DvP_Mult"] = merged["DvP_Mult"].fillna(1.00).round(3)
        merged["DvP_Tier"] = np.where(merged["Opp"] == "BYE", "⏸️ BYE", merged["DvP_Tier"].fillna("🟡 Neutral"))

        po_df = self.calculate_playoff_sos(dvp_df)
        merged = pd.merge(merged, po_df, on=["Team", "Pos"], how="left")
        merged["Playoff_Slate"] = merged["Playoff_Slate"].fillna("TBD → TBD → TBD")
        for c in ["Wk15_Mult", "Wk16_Mult", "Wk17_Mult", "Playoff_Mult"]:
            merged[c] = merged[c].fillna(1.00).round(3)
        merged["Playoff_Boost_Pct"] = merged["Playoff_Boost_Pct"].fillna(0.0).round(1)
        merged["Playoff_SOS_Rank"] = merged["Playoff_SOS_Rank"].fillna(16).astype(int)
        merged["Playoff_Tier"] = merged["Playoff_Tier"].fillna("⚖️ Neutral Slate")

        v_mult = np.where(merged["Opp"] == "BYE", 0.0, 1.0 + (merged["Vegas_Implied"] - 22.5) * 0.020)
        merged["Proj_Pts"] = (merged["ROS_PPG"] * v_mult * merged["DvP_Mult"]).round(1)
        scaled_std = merged["Std_Dev_Actual"] * np.sqrt(merged["DvP_Mult"])
        merged["Floor"] = np.where(merged["Opp"] == "BYE", 0.0, np.maximum(0.0, merged["Proj_Pts"] - 1.28 * scaled_std)).round(1)
        merged["Ceiling"] = np.where(merged["Opp"] == "BYE", 0.0, merged["Proj_Pts"] + 1.28 * scaled_std).round(1)

        merged["Breakout_Index"] = (
            (merged["WOPR"] * 45.0)
            + (merged["Snap_%"] * 0.35)
            + np.maximum(0, merged["Snap_Trend_2W"] * 1.2)
            + ((merged["DvP_Mult"] - 1.0) * 20.0)
            + ((merged["Playoff_Mult"] - 1.0) * 25.0)
        ).round(1)

        dst_df = self.build_dst_streaming_dataframe(weekly, vegas_df, vegas_n1)
        k_df = self.build_kicker_streaming_dataframe(vegas_df, dst_df)
        return pd.concat([merged, dst_df, k_df], ignore_index=True)


# ==============================================================================
# 3. CBS FANTASY v3.0 API SYNC CONNECTOR
# ==============================================================================
class CBSFantasySync:
    def __init__(self, league_id: str, my_team_id: str = "1", cbs_sess_cookie: Optional[str] = None):
        self.league_id = league_id.strip()
        self.my_team_id = str(my_team_id).strip()
        self.cbs_sess = cbs_sess_cookie.strip() if cbs_sess_cookie else None
        self.league_domain = f"https://{self.league_id}.football.cbssports.com"
        self.api_base = "https://api.cbssports.com/fantasy"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        })
        if self.cbs_sess:
            self.session.cookies.set("CBS_SESS", self.cbs_sess, domain=".cbssports.com")

    @staticmethod
    def normalize_name(name: str) -> str:
        return NFLUsageEngine.normalize_name(name)

    def _extract_bearer_token(self) -> Optional[str]:
        resp = self.session.get(f"{self.league_domain}/teams", timeout=12)
        if resp.status_code != 200 or "/login" in resp.url:
            raise ConnectionError("CBS session cookie expired or invalid. Paste a fresh CBS_SESS cookie or click Refresh Cookie.")
        for pat in [r'"access_token"\s*:\s*"([^"]+)"', r"access_token\s*=\s*['\"]([^'\"]+)['\"]", r"token\s*:\s*['\"]([a-zA-Z0-9_\-\.]{20,})['\"]"]:
            m = re.search(pat, resp.text)
            if m:
                return m.group(1)
        return None

    def fetch_rosters_and_faab(self, token: Optional[str] = None) -> Tuple[pd.DataFrame, Dict]:
        params = {"version": "3.0", "league_id": self.league_id, "response_format": "json"}
        if token:
            params["access_token"] = token
        t_resp = self.session.get(f"{self.api_base}/league/teams", params=params, timeout=12)
        teams_data = t_resp.json().get("body", {}).get("teams", [])

        faab_list, my_faab = [], 68
        for t in teams_data:
            t_id = str(t.get("id", ""))
            bal = float(t.get("faab_balance", t.get("waiver_budget", 100)))
            faab_list.append(bal)
            if t_id == self.my_team_id:
                my_faab = int(bal)

        params["team_id"] = "all"
        r_resp = self.session.get(f"{self.api_base}/league/rosters", params=params, timeout=12)
        teams_rosters = r_resp.json().get("body", {}).get("rosters", {}).get("teams", [])

        rows = []
        valid_pos = {"QB", "RB", "WR", "TE", "K", "DEF", "DST"}
        for team in teams_rosters:
            t_id = str(team.get("id", ""))
            status = "My Roster" if t_id == self.my_team_id else "Opponent Roster"
            for p in team.get("players", []):
                pos = p.get("eligible_positions", [p.get("position", "")])[0]
                pos = "DEF" if pos == "DST" else pos
                if pos not in valid_pos:
                    continue
                fname = p.get("fullname", f"{p.get('firstname', '')} {p.get('lastname', '')}").strip()
                rows.append({
                    "Player": fname, "Clean_Name": self.normalize_name(fname), "Pos": pos,
                    "Team": p.get("pro_team", "FA"), "Status": status,
                    "CBS_Proj": float(p.get("projected_points", p.get("ytd_points", 10.0)))
                })

        meta = {"my_faab": my_faab, "league_avg_faab": int(round(np.mean(faab_list))) if faab_list else 54, "teams_synced": len(teams_rosters)}
        return pd.DataFrame(rows), meta

    def fetch_waiver_pool(self, token: Optional[str] = None, limit: int = 100) -> pd.DataFrame:
        params = {"version": "3.0", "league_id": self.league_id, "status": "free_agent", "response_format": "json"}
        if token:
            params["access_token"] = token
        resp = self.session.get(f"{self.api_base}/players/list", params=params, timeout=12)
        players = resp.json().get("body", {}).get("players", [])
        rows = []
        valid_pos = {"QB", "RB", "WR", "TE", "K", "DEF", "DST"}
        for p in players[:limit]:
            pos = p.get("eligible_positions", [p.get("position", "")])[0]
            pos = "DEF" if pos == "DST" else pos
            if pos not in valid_pos:
                continue
            fname = p.get("fullname", "").strip()
            rows.append({
                "Player": fname, "Clean_Name": self.normalize_name(fname), "Pos": pos,
                "Team": p.get("pro_team", "FA"), "Status": "Waiver",
                "CBS_Proj": float(p.get("projected_points", 8.5))
            })
        return pd.DataFrame(rows)

    def fetch_draft_results(self, token: Optional[str] = None) -> pd.DataFrame:
        params = {"version": "3.0", "league_id": self.league_id, "response_format": "json"}
        if token:
            params["access_token"] = token
        try:
            resp = self.session.get(f"{self.api_base}/league/draft/results", params=params, timeout=12)
            picks = resp.json().get("body", {}).get("draft_results", {}).get("picks", [])
        except Exception:
            picks = []
        rows = []
        for p in picks:
            p_obj = p.get("player", {})
            fname = p_obj.get("fullname", "").strip()
            pos = p_obj.get("eligible_positions", [p_obj.get("position", "")])[0]
            pos = "DEF" if pos == "DST" else pos
            rows.append({"Clean_Name": self.normalize_name(fname), "Pos": pos, "Draft_Round": int(p.get("round", 12))})
        if not rows:
            return pd.DataFrame(columns=["Clean_Name", "Pos", "Draft_Round"])
        return pd.DataFrame(rows).drop_duplicates(subset=["Clean_Name", "Pos"], keep="first")

    def sync_all(self, usage_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict]:
        token = self._extract_bearer_token()
        rosters_df, faab_meta = self.fetch_rosters_and_faab(token)
        waivers_df = self.fetch_waiver_pool(token, limit=100)
        draft_df = self.fetch_draft_results(token)

        combined = pd.concat([rosters_df, waivers_df], ignore_index=True)
        usage_clean = usage_df.copy()
        if "Clean_Name" not in usage_clean.columns:
            usage_clean["Clean_Name"] = usage_clean["Player"].apply(self.normalize_name)

        merged = pd.merge(
            combined,
            usage_clean.drop(columns=["Player", "Team", "Status", "player_id"], errors="ignore"),
            on=["Clean_Name", "Pos"], how="left"
        )
        merged = pd.merge(merged, draft_df, on=["Clean_Name", "Pos"], how="left")
        merged["Draft_Round"] = merged["Draft_Round"].fillna(12).astype(int)

        merged["Opp"] = merged["Opp"].fillna("TBD")
        merged["Vegas_Implied"] = merged["Vegas_Implied"].fillna(21.5)
        merged["Proj_Pts"] = merged["Proj_Pts"].fillna(merged["CBS_Proj"]).round(1)
        merged["ROS_PPG"] = merged["ROS_PPG"].fillna(merged["Proj_Pts"] * 0.96).round(1)
        merged["Proj_Rec"] = merged["Proj_Rec"].fillna(np.where(merged["Pos"].isin(["WR", "TE"]), 3.2, np.where(merged["Pos"] == "RB", 2.0, 0.0))).round(1)
        merged["Floor"] = merged["Floor"].fillna(merged["Proj_Pts"] * 0.62).round(1)
        merged["Ceiling"] = merged["Ceiling"].fillna(merged["Proj_Pts"] * 1.48).round(1)
        merged["Snap_%"] = merged["Snap_%"].fillna(50).astype(int)
        merged["Snap_Trend_2W"] = merged["Snap_Trend_2W"].fillna(0.0).round(1)
        merged["Target_%"] = merged["Target_%"].fillna(10.0).round(1)
        merged["Air_Yards_%"] = merged["Air_Yards_%"].fillna(8.0).round(1)
        merged["WOPR"] = merged["WOPR"].fillna(0.22).round(2)
        merged["DvP_Rank"] = merged["DvP_Rank"].fillna(16).astype(int)
        merged["DvP_Diff_Pct"] = merged["DvP_Diff_Pct"].fillna(0.0).round(1)
        merged["DvP_Mult"] = merged["DvP_Mult"].fillna(1.00).round(3)
        merged["DvP_Tier"] = merged["DvP_Tier"].fillna("🟡 Neutral")
        merged["Breakout_Index"] = merged["Breakout_Index"].fillna(25.0).round(1)

        for col, default in [
            ("Playoff_Slate", "TBD → TBD → TBD"), ("Wk15_Mult", 1.00), ("Wk16_Mult", 1.00),
            ("Wk17_Mult", 1.00), ("Playoff_Mult", 1.00), ("Playoff_Boost_Pct", 0.0),
            ("Playoff_SOS_Rank", 16), ("Playoff_Tier", "⚖️ Neutral Slate"),
            ("WkN1_Opp", "TBD"), ("WkN1_Opp_Implied", 22.0), ("WkN1_Opp_Sack_Rate_Pct", 7.2),
            ("WkN1_Proj_Pts", 0.0), ("Two_Wk_Proj_Pts", 0.0), ("Stash_Tag", "N/A")
        ]:
            if col not in merged.columns:
                merged[col] = default
            else:
                merged[col] = merged[col].fillna(default)

        return merged, faab_meta


# ==============================================================================
# 4. SESSION STATE & FALLBACK SAMPLE DATA
# ==============================================================================
if "synced_df" not in st.session_state:
    st.session_state["synced_df"] = None
if "my_faab" not in st.session_state:
    st.session_state["my_faab"] = 68
if "opp_avg_faab" not in st.session_state:
    st.session_state["opp_avg_faab"] = 54


@st.cache_data(ttl=21600, show_spinner="Fetching live NFL play-by-play, WOPR, PFR snaps, DvP & Playoff SOS...")
def load_live_nfl_usage(season: int, upcoming_week: int) -> pd.DataFrame:
    engine = NFLUsageEngine(season=season)
    return engine.build_usage_dataframe(upcoming_week=upcoming_week)


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
# 5. SIDEBAR CONTROLS & LIVE CBS SYNC TRIGGER
# ==============================================================================
st.sidebar.title("🥃 Whiskey Power Control")
current_season = datetime.now().year
current_week = st.sidebar.slider("Current NFL Week", min_value=1, max_value=17, value=7)

strategy = st.sidebar.radio(
    "Matchup Game Script (MILP Mode):",
    ["Balanced (Max EV)", "Protect Lead (High Floor)", "Chasing Points (High Ceiling)"],
)
lam = {"Balanced (Max EV)": 0.0, "Protect Lead (High Floor)": -0.35, "Chasing Points (High Ceiling)": 0.50}[strategy]

with st.sidebar.expander("🔐 CBS Auto-Auth & Live Sync", expanded=True):
    cbs_league_id = st.text_input("CBS League ID", value=get_config_secret("CBS_LEAGUE_ID", "my-14team-ppr"))
    cbs_team_id = st.text_input("Your CBS Team ID (1-14)", value=get_config_secret("CBS_TEAM_ID", "1"))
    manual_cookie = st.text_input("CBS_SESS Cookie (Optional Override)", value=get_config_secret("CBS_SESS", ""), type="password")

    col_auth, col_sync = st.columns(2)
    with col_auth:
        if st.button("🔑 Auto-Login", width="stretch"):
            with st.spinner("Refreshing CBS_SESS via Playwright..."):
                try:
                    res = refresh_cbs_session(league_id=cbs_league_id, headless=True)
                    st.success(f"Refreshed ({res['CBS_LAST_REFRESH']})")
                    st.rerun()
                except Exception as err:
                    st.error(f"{err}")

    with col_sync:
        if st.button("🚀 Sync CBS", width="stretch"):
            with st.spinner("Syncing 14-team CBS rosters, FAAB, Draft & nfl_data_py..."):
                try:
                    cookie_val = manual_cookie or get_config_secret("CBS_SESS", "")
                    if not cookie_val:
                        fresh = refresh_cbs_session(league_id=cbs_league_id, headless=True)
                        cookie_val = fresh["CBS_SESS"]
                    base_usage_df = load_live_nfl_usage(season=current_season, upcoming_week=current_week)
                    connector = CBSFantasySync(cbs_league_id, cbs_team_id, cookie_val)
                    synced_df, faab_meta = connector.sync_all(base_usage_df)
                    st.session_state["synced_df"] = synced_df
                    st.session_state["my_faab"] = faab_meta["my_faab"]
                    st.session_state["opp_avg_faab"] = faab_meta["league_avg_faab"]
                    st.success(f"Synced {faab_meta['teams_synced']} teams! FAAB: ${faab_meta['my_faab']}")
                except Exception as e:
                    st.error(f"Sync error: {e}")

    uploaded_csv = st.file_uploader("Or Drop CBS / FantasyPros CSV", type=["csv"])
    if uploaded_csv is not None:
        st.session_state["synced_df"] = pd.read_csv(uploaded_csv)
        st.success("Loaded custom league CSV!")

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
# 6. QUANTITATIVE ENRICHMENT & MILP LINEUP SOLVER
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
df["VORP_14T"] = np.maximum(0.5, (df["ROS_PPG"] - df["Pos"].map(replacement_baseline)) * df["Pos"].map(scarcity_mult)).round(2)
df["Playoff_VORP_14T"] = np.maximum(0.5, (df["Playoff_PPG"] - df["Pos"].map(replacement_baseline)) * df["Pos"].map(scarcity_mult)).round(2)
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


def optimize_lineup(roster_df: pd.DataFrame, score_col: str = "Opt_Score") -> pd.DataFrame:
    out = roster_df.copy()
    n = len(out)
    if n == 0:
        out["Decision"] = []
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
    return out


# ==============================================================================
# 7. MAIN HEADER & 7-TAB INTERFACE
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
starters = opt_df[opt_df["Decision"] == "START"].sort_values(by="Pos")
bench = opt_df[opt_df["Decision"] == "SIT"].sort_values(by="Opt_Score", ascending=False)

with tab1:
    c_start, c_bench = st.columns([3, 2])
    with c_start:
        st.markdown("#### 🟢 Starting Lineup (1QB, 2RB, 2WR, 1TE, 1Flex, 1K, 1DEF)")
        s_cols = [c for c in ["Pos", "Player", "Team", "Opp", "DvP_Rank", "DvP_Tier", "Proj_Rec", "Proj_Pts", "Floor", "Ceiling", "Opt_Score"] if c in starters.columns]
        st.dataframe(starters[s_cols], width="stretch", hide_index=True)
        m1, m2, m3 = st.columns(3)
        m1.metric("Projected PPR Total", f"{starters['Proj_Pts'].sum():.1f} pts")
        m2.metric("Floor (10th Pct)", f"{starters['Floor'].sum():.1f} pts")
        m3.metric("Ceiling (90th Pct)", f"{starters['Ceiling'].sum():.1f} pts")
    with c_bench:
        st.markdown("#### 🔴 Recommended Bench (Sit)")
        b_cols = [c for c in ["Pos", "Player", "Opp", "DvP_Tier", "Proj_Rec", "Proj_Pts", "Floor", "Ceiling", "Opt_Score"] if c in bench.columns]
        st.dataframe(bench[b_cols], width="stretch", hide_index=True)

with tab2:
    f1, f2, f3 = st.columns(3)
    pos_f = f1.multiselect("Positions:", ["QB", "RB", "WR", "TE"], default=["RB", "WR", "TE"])
    min_snap = f2.slider("Min 2-Week Snap % Surge:", -20.0, 30.0, 0.0, 2.5)
    min_wopr = f3.slider("Min WOPR:", 0.0, 0.70, 0.15, 0.05)
    w_pool = waivers_all[(waivers_all["Pos"].isin(pos_f)) & (waivers_all["Snap_Trend_2W"] >= min_snap) & ((waivers_all["WOPR"] >= min_wopr) | (waivers_all["Pos"].isin(["RB", "QB"])))].sort_values(by="Breakout_Index", ascending=False)
    w_cols = [c for c in ["Pos", "Player", "Team", "Opp", "Snap_%", "Snap_Trend_2W", "Target_%", "WOPR", "ROS_PPG", "Playoff_Tier", "Keeper_Tag", "Breakout_Index", "VORP_14T"] if c in w_pool.columns]
    st.dataframe(w_pool[w_cols], width="stretch", hide_index=True)

with tab3:
    cl, cr = st.columns(2)
    skill_w = waivers_all[waivers_all["Pos"].isin(["QB", "RB", "WR", "TE"])]["Player"].tolist() or waivers_all["Player"].tolist()
    with cl:
        target_w = st.selectbox("Select Waiver Target:", skill_w)
        w_row = waivers_all[waivers_all["Player"] == target_w].iloc[0]
        def_idx = 0 if w_row["VORP_14T"] >= 5.5 else (1 if w_row["VORP_14T"] >= 3.2 else 2)
        t_choice = st.selectbox("Usage Tier:", ["League Winner / Bellcow Takeover (Base 60%)", "Every-Week PPR Starter / High Target Slot (Base 30%)", "Flex Depth / High-Ceiling Handcuff (Base 12%)", "1-Week Injury or Bye Streamer (Base 6%)"], index=def_idx)
        n_choice = st.select_slider("Positional Need:", options=["Luxury Stash (0.5x)", "Bench Depth (0.75x)", "Flex Upgrade (1.0x)", "Desperate Starter Hole (1.35x)"], value="Flex Upgrade (1.0x)")
        drop_cand = st.selectbox("Drop Candidate:", bench["Player"].tolist() if not bench.empty else my_roster["Player"].tolist())
        drop_row = my_roster[my_roster["Player"] == drop_cand].iloc[0]

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
        send_p = st.multiselect("📤 You Send:", my_sk, default=[p for p in ["Chase Brown", "Wan'Dale Robinson"] if p in my_sk][:2])
    with tc2:
        recv_p = st.multiselect("📥 You Receive:", opp_sk, default=[p for p in ["Drake London", "Jahmyr Gibbs"] if p in opp_sk][:1])

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

        st.dataframe(pd.concat([s_df.assign(Side="📤 Send"), r_df.assign(Side="📥 Receive")])[["Side", "Player", "Pos", "ROS_PPG", "Playoff_Slate", "Playoff_PPG", "Playoff_Tier", "Keeper_Tag", "Trade_Value"]], width="stretch", hide_index=True)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Send Value", f"{s_df['Trade_Value'].sum():.1f}")
        k2.metric("Effective Get Value", f"{r_df['Trade_Value'].sum() + add_v - drop_v:.1f}")
        k3.metric("Reg Season Starting 9 Δ", f"{reg_delta:+.2f} PPG")
        k4.metric("🏆 Playoff Starting 9 Δ", f"{po_delta:+.2f} PPG")

with tab5:
    d_mode = st.radio("DST Lens:", ["🔥 Best 2-Week Smash Holds", "💎 $0 FAAB Next-Week Pre-Stashes", "⚡ Best Week N Streamer"], horizontal=True)
    d_pool = df[(df["Pos"] == "DEF") & (df["Status"].isin(["My Roster", "Waiver"]))].copy()
    sort_c = "Two_Wk_Proj_Pts" if "2-Week" in d_mode else ("WkN1_Proj_Pts" if "$0 FAAB" in d_mode else "Proj_Pts")
    st.dataframe(d_pool.sort_values(by=sort_c, ascending=False)[["Player", "Status", "Stash_Tag", "Opp", "Opp_Vegas_Implied", "Opp_Sack_Rate_Pct", "Proj_Pts", "WkN1_Opp", "WkN1_Opp_Implied", "WkN1_Proj_Pts", "Two_Wk_Proj_Pts"]], width="stretch", hide_index=True)

    st.markdown("#### 🎯 Top Kicker Streamers")
    k_pool = df[(df["Pos"] == "K") & (df["Status"].isin(["My Roster", "Waiver"]))].sort_values(by="Proj_Pts", ascending=False)
    st.dataframe(k_pool[["Player", "Team", "Status", "Opp", "Vegas_Implied", "Roof", "Proj_FGs", "Proj_Pts", "DvP_Tier"]], width="stretch", hide_index=True)

with tab6:
    my_k = my_roster[my_roster["Keeper_Eligible"]].sort_values(by="Keeper_Surplus", ascending=False)
    st.dataframe(my_k[["Player", "Pos", "Team", "Draft_Round", "Keeper_Cost_Rd", "ROS_PPG", "VORP_14T", "Proj_Next_Yr_Rd", "Keeper_Surplus", "Keeper_Tag"]], width="stretch", hide_index=True)

with tab7:
    prompt_text = f"""You are an elite 14-Team Full-PPR Fantasy Football Analyst for Whiskey Power (FEFFL).
Week {current_week} | Mode: {strategy} | FAAB: ${faab_rem}/${faab_start}
Starters: {starters[['Pos', 'Player', 'Opp', 'Proj_Pts', 'Floor', 'Ceiling']].to_dict(orient='records')}
Bench: {bench[['Pos', 'Player', 'Opp', 'Proj_Pts', 'ROS_PPG', 'Playoff_PPG']].to_dict(orient='records')}
Waiver Target: {target_w} (Bid: ${rec_bid}, Drop: {drop_cand})
Trade Eval: Send {send_p} for {recv_p} (Reg Δ: {reg_delta if (send_p and recv_p) else 0:+.2f} PPG, Playoff Δ: {po_delta if (send_p and recv_p) else 0:+.2f} PPG)"""
    st.code(prompt_text, language="markdown")