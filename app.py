import streamlit as st
import pandas as pd
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds
import requests

st.set_page_config(page_title="14-Team PPR Edge Optimizer", layout="wide")
st.title("🏈 14-Team CBS PPR Optimizer & Waiver AI")

# --- SIDEBAR: LEAGUE & MATCHUP CONTEXT ---
st.sidebar.header("⚙️ Matchup Strategy & Sync")
strategy = st.sidebar.radio(
    "Matchup Game Script:",
    ["Balanced (Max EV)", "Protect Lead (High Floor)", "Chasing Points (High Ceiling)"],
    help="Adjusts the optimizer toward high-reception floor plays or high-air-yard boom plays."
)

lambda_map = {
    "Balanced (Max EV)": 0.0,
    "Protect Lead (High Floor)": -0.35,
    "Chasing Points (High Ceiling)": 0.50
}
lam = lambda_map[strategy]

with st.sidebar.expander("🔗 CBS League Sync Settings"):
    cbs_league_id = st.text_input("CBS League ID", value="my-14team-ppr")
    cbs_cookie = st.text_input("CBS_SESS Cookie (Optional)", type="password")
    st.caption("Tip: Grab CBS_SESS from Chrome DevTools -> Application -> Cookies on cbssports.com")

# --- SAMPLE DATA: YOUR ROSTER + 14-TEAM WAIVER POOL ---
@st.cache_data
def load_sample_data():
    roster_data = [
        # Name, Pos, Team, Opp, Vegas_Total, Proj_Rec, Proj_Pts, Floor, Ceiling, Snap_Pct, Target_Share, Status
        ("Jayden Daniels", "QB", "WAS", "BAL", 26.5, 0.0, 21.4, 15.2, 29.8, 100, 0.0, "Rostered"),
        ("Caleb Williams", "QB", "CHI", "JAX", 24.0, 0.0, 18.2, 11.0, 26.5, 100, 0.0, "Rostered"),
        ("Breece Hall", "RB", "NYJ", "BUF", 20.5, 4.8, 17.5, 11.2, 25.4, 78, 16.5, "Rostered"),
        ("Chase Brown", "RB", "CIN", "NYG", 25.0, 3.5, 14.2, 9.5, 21.0, 64, 12.0, "Rostered"),
        ("Tyjae Spears", "RB", "TEN", "IND", 19.5, 4.1, 11.4, 7.8, 16.2, 52, 14.2, "Rostered"),
        ("Amon-Ra St. Brown", "WR", "DET", "DAL", 27.5, 7.8, 20.6, 14.5, 28.0, 92, 29.5, "Rostered"),
        ("Wan'Dale Robinson", "WR", "NYG", "CIN", 22.0, 6.9, 13.8, 10.4, 17.5, 76, 26.8, "Rostered"),
        ("Rashid Shaheed", "WR", "NO", "TB", 20.0, 3.2, 12.9, 4.2, 24.8, 81, 18.5, "Rostered"),
        ("Xavier Worthy", "WR", "KC", "SF", 24.5, 3.8, 12.4, 5.5, 23.0, 68, 16.0, "Rostered"),
        ("Trey McBride", "TE", "ARI", "GB", 23.0, 6.2, 14.5, 10.1, 20.2, 94, 25.4, "Rostered"),
        ("Brandon Aubrey", "K", "DAL", "DET", 24.5, 0.0, 9.8, 6.0, 15.0, 100, 0.0, "Rostered"),
        ("Broncos DST", "DEF", "DEN", "LAC", 18.5, 0.0, 8.4, 4.0, 14.0, 100, 0.0, "Rostered"),
    ]
    
    waiver_data = [
        # Deep 14-team waiver targets with high underlying usage
        ("Kimani Vidal", "RB", "LAC", "DEN", 21.0, 3.2, 10.8, 6.2, 17.5, 48, 11.5, "Waiver"),
        ("Roschon Johnson", "RB", "CHI", "JAX", 24.0, 2.1, 9.5, 4.0, 16.0, 42, 7.5, "Waiver"),
        ("DeMario Douglas", "WR", "NE", "HOU", 18.5, 5.8, 12.6, 8.5, 17.8, 79, 24.2, "Waiver"),
        ("Jalen Tolbert", "WR", "DAL", "DET", 24.5, 4.2, 11.9, 6.5, 19.4, 86, 17.8, "Waiver"),
        ("Cade Otton", "TE", "TB", "NO", 23.5, 4.9, 10.4, 6.8, 15.2, 91, 18.0, "Waiver"),
    ]
    
    cols = ["Player", "Pos", "Team", "Opp", "Vegas_Implied", "Proj_Rec", 
            "Proj_Pts", "Floor", "Ceiling", "Snap_%", "Target_%", "Status"]
    return pd.DataFrame(roster_data + waiver_data, columns=cols)

df = load_sample_data()

# Calculate Volatility & Risk-Adjusted Score + 14-Team VORP
replacement_baseline = {"QB": 15.5, "RB": 8.2, "WR": 9.0, "TE": 7.5, "K": 7.5, "DEF": 6.5}
df["Std_Dev"] = (df["Ceiling"] - df["Floor"]) / 2.56
df["Opt_Score"] = (df["Proj_Pts"] + (lam * df["Std_Dev"])).round(2)
df["VORP_14T"] = (df["Proj_Pts"] - df["Pos"].map(replacement_baseline)).round(2)

# --- MILP LINEUP OPTIMIZER (1QB, 2RB, 2WR, 1TE, 1FLEX, 1K, 1DEF) ---
def optimize_lineup(roster_df):
    n = len(roster_df)
    c = -roster_df["Opt_Score"].values  # Minimize negative = Maximize
    
    is_qb = (roster_df["Pos"] == "QB").astype(int).values
    is_rb = (roster_df["Pos"] == "RB").astype(int).values
    is_wr = (roster_df["Pos"] == "WR").astype(int).values
    is_te = (roster_df["Pos"] == "TE").astype(int).values
    is_flex = roster_df["Pos"].isin(["RB", "WR", "TE"]).astype(int).values
    is_k = (roster_df["Pos"] == "K").astype(int).values
    is_def = (roster_df["Pos"] == "DEF").astype(int).values
    
    # Constraints matrix
    A = np.vstack([is_qb, is_rb, is_wr, is_te, is_flex, is_k, is_def])
    # Exact bounds: QB=1, RB=[2,3], WR=[2,3], TE=[1,2], Total RB+WR+TE=6, K=1, DEF=1
    lb = [1, 2, 2, 1, 6, 1, 1]
    ub = [1, 3, 3, 2, 6, 1, 1]
    
    res = milp(c=c, integrality=np.ones(n), bounds=Bounds(0, 1), 
               constraints=LinearConstraint(A, lb, ub))
    
    roster_df = roster_df.copy()
    roster_df["Decision"] = ["START" if val > 0.5 else "SIT" for val in res.x]
    return roster_df

# --- UI TABS ---
tab1, tab2, tab3 = st.tabs(["📊 Start/Sit Optimizer", "🔥 14-Team Waiver Radar", "🧠 True AI Coach Brief"])

with tab1:
    st.subheader(f"Optimal Starting 9 ({strategy})")
    rostered = df[df["Status"] == "Rostered"]
    opt_df = optimize_lineup(rostered)
    
    col1, col2 = st.columns([3, 2])
    with col1:
        st.markdown("**🟢 Starting Lineup**")
        starters = opt_df[opt_df["Decision"] == "START"].sort_values(by="Pos")
        st.dataframe(starters[["Pos", "Player", "Team", "Opp", "Proj_Rec", "Proj_Pts", "Floor", "Ceiling", "Opt_Score"]], use_container_index=True)
        st.metric("Total Projected PPR Points", f"{starters['Proj_Pts'].sum():.1f} pts", 
                  delta=f"Ceiling: {starters['Ceiling'].sum():.1f} pts")
    with col2:
        st.markdown("**🔴 Bench (Sit)**")
        bench = opt_df[opt_df["Decision"] == "SIT"]
        st.dataframe(bench[["Pos", "Player", "Proj_Rec", "Proj_Pts", "Floor", "Ceiling", "Opt_Score"]], use_container_index=True)
        st.info("💡 **Flex Call Notice:** Switch between 'Protect Lead' and 'Chasing Points' in the sidebar to see how Wan'Dale Robinson (6.9 rec PPR floor) swaps with Rashid Shaheed (deep threat ceiling).")

with tab2:
    st.subheader("14-Team PPR Waiver Wire Upgrade Engine")
    st.markdown("Flags unrostered players whose **Target %**, **Snap %**, and **14-Team VORP** outpace your worst bench pieces.")
    waivers = df[df["Status"] == "Waiver"].sort_values(by="VORP_14T", ascending=False)
    st.dataframe(waivers[["Pos", "Player", "Team", "Opp", "Proj_Rec", "Proj_Pts", "Snap_%", "Target_%", "VORP_14T"]], use_container_index=True)

with tab3:
    st.subheader("Context-Injected AI Coach Prompt")
    st.markdown("FantasyPros AI fails because it lacks deep usage context. Feed this auto-generated brief to an LLM API (OpenAI/Anthropic) for sharp analysis:")
    prompt_text = f"""You are an elite high-stakes 14-team Full-PPR Fantasy Football Analyst.
My Strategy Mode: {strategy}
Starting Lineup: {starters[['Player', 'Pos', 'Proj_Pts', 'Proj_Rec', 'Floor', 'Ceiling']].to_dict(orient='records')}
Bench: {bench[['Player', 'Pos', 'Proj_Pts', 'Proj_Rec', 'Floor', 'Ceiling']].to_dict(orient='records')}
Top Waiver Targets: {waivers[['Player', 'Pos', 'Proj_Pts', 'Snap_%', 'Target_%']].to_dict(orient='records')}

Tasks:
1. Evaluate my Flex decision between high-reception floor (Wan'Dale Robinson) and air-yards ceiling (Rashid Shaheed / Xavier Worthy) given my {strategy} mode.
2. Identify the highest-upside waiver wire swap for a 14-team PPR format based on Target Share and Snap % rather than raw box score points."""
    st.code(prompt_text, language="markdown")
