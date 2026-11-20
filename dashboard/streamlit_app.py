"""
Streamlit Dashboard for the YouTube Automation System.

Provides a visual interface for:
  • Topic input & generation
  • Step-by-step or one-click pipeline execution
  • YouTube upload with privacy & scheduling
  • Video history with status tracking
  • Real-time log viewing
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import httpx
import streamlit as st

# ── Configuration ────────────────────────────────────────────────────

API_BASE = "http://localhost:8000"
TIMEOUT = 300  # 5-minute timeout for long-running pipeline operations

st.set_page_config(
    page_title="YouTube Automation Dashboard",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ───────────────────────────────────────────────────────

st.markdown("""
<style>
    /* Import Google Font */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    /* Global */
    .stApp {
        font-family: 'Inter', sans-serif;
    }

    /* Header styling */
    .main-header {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 2rem;
        border-radius: 16px;
        margin-bottom: 2rem;
        text-align: center;
        box-shadow: 0 8px 32px rgba(102, 126, 234, 0.3);
    }
    .main-header h1 {
        color: white;
        font-size: 2.2rem;
        font-weight: 700;
        margin: 0;
        text-shadow: 0 2px 4px rgba(0,0,0,0.2);
    }
    .main-header p {
        color: rgba(255,255,255,0.85);
        font-size: 1.1rem;
        margin-top: 0.5rem;
    }

    /* Card styling */
    .pipeline-card {
        background: linear-gradient(145deg, #1e1e2e, #2a2a3e);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px;
        padding: 1.5rem;
        margin-bottom: 1rem;
        transition: transform 0.2s, box-shadow 0.2s;
    }
    .pipeline-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 24px rgba(0,0,0,0.3);
    }

    /* Status badges */
    .status-badge {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.8rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .status-pending { background: #ffd43b22; color: #ffd43b; border: 1px solid #ffd43b44; }
    .status-generating { background: #339af022; color: #339af0; border: 1px solid #339af044; }
    .status-ready { background: #51cf6622; color: #51cf66; border: 1px solid #51cf6644; }
    .status-uploading { background: #845ef722; color: #845ef7; border: 1px solid #845ef744; }
    .status-uploaded { background: #20c99722; color: #20c997; border: 1px solid #20c99744; }
    .status-failed { background: #ff6b6b22; color: #ff6b6b; border: 1px solid #ff6b6b44; }

    /* Step indicator */
    .step-indicator {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 32px;
        height: 32px;
        border-radius: 50%;
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        font-weight: 700;
        font-size: 0.9rem;
        margin-right: 0.75rem;
    }

    /* Sidebar styling */
    .sidebar-section {
        background: rgba(255,255,255,0.03);
        border-radius: 12px;
        padding: 1rem;
        margin-bottom: 1rem;
    }

    /* Hide default streamlit elements */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* Metric cards */
    [data-testid="stMetricValue"] {
        font-size: 2rem;
        font-weight: 700;
    }

    /* Button styling */
    .stButton > button {
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.2s;
    }
    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.2);
    }
</style>
""", unsafe_allow_html=True)


# ── Helper functions ─────────────────────────────────────────────────

def api_call(method: str, endpoint: str, **kwargs) -> dict | None:
    """Make an API call to the FastAPI backend."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = getattr(client, method)(f"{API_BASE}{endpoint}", **kwargs)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        st.error("❌ Cannot connect to API server. Make sure it's running on port 8000.")
        return None
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", str(exc))
        st.error(f"❌ API error: {detail}")
        return None
    except Exception as exc:
        st.error(f"❌ Unexpected error: {exc}")
        return None


def status_badge(status: str) -> str:
    """Return an HTML status badge."""
    return f'<span class="status-badge status-{status}">{status}</span>'


# ── Header ───────────────────────────────────────────────────────────

st.markdown("""
<div class="main-header">
    <h1>🎬 YouTube Automation Studio</h1>
    <p>AI-powered video generation & upload pipeline</p>
</div>
""", unsafe_allow_html=True)


# ── Sidebar ──────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### ⚙️ Configuration")

    category = st.selectbox(
        "Video Category",
        [
            "Technology", "AI", "Science", "Finance",
            "Education", "Productivity", "Health",
            "Entertainment", "Gaming", "News",
        ],
        index=0,
    )

    # Fetch voices
    voices_data = api_call("get", "/voices")
    voice_options = ["Default (en-US-AvaNeural)"]
    if voices_data and "voices" in voices_data:
        voice_options += [v["name"] for v in voices_data["voices"]]
    voice = st.selectbox("TTS Voice", voice_options)
    selected_voice = None if voice.startswith("Default") else voice

    video_format = st.radio(
        "Video Format",
        ["horizontal", "vertical"],
        format_func=lambda x: "🖥️ Horizontal (16:9)" if x == "horizontal" else "📱 Vertical (9:16)",
    )

    st.markdown("---")
    st.markdown("### 📊 Quick Stats")

    history = api_call("get", "/history")
    if history:
        total = history.get("total", 0)
        videos_list = history.get("videos", [])
        uploaded = sum(1 for v in videos_list if v.get("upload_status") == "uploaded")
        ready = sum(1 for v in videos_list if v.get("upload_status") == "ready")
        failed = sum(1 for v in videos_list if v.get("upload_status") == "failed")

        col1, col2 = st.columns(2)
        col1.metric("Total", total)
        col2.metric("Uploaded", uploaded)
        col1.metric("Ready", ready)
        col2.metric("Failed", failed)


# ── Main Content ─────────────────────────────────────────────────────

tab1, tab2, tab3, tab4 = st.tabs([
    "🚀 Generate",
    "📤 Upload",
    "📜 History",
    "📋 Logs",
])


# ── Tab 1: Generate ─────────────────────────────────────────────────

with tab1:
    st.markdown("### 💡 Topic")
    col_topic, col_auto = st.columns([3, 1])
    with col_topic:
        custom_topic = st.text_input(
            "Enter a custom topic (or leave blank for AI generation)",
            placeholder="e.g., 10 Mind-Blowing AI Tools You Didn't Know Existed",
        )
    with col_auto:
        st.markdown("<br>", unsafe_allow_html=True)
        auto_topic = st.button("🎲 Auto-Generate", use_container_width=True)

    st.markdown("---")

    # One-click full pipeline
    st.markdown("### 🎯 One-Click Generation")
    if st.button("⚡ Generate Full Video", type="primary", use_container_width=True):
        with st.spinner("Running full pipeline — this may take several minutes..."):
            progress = st.progress(0, text="Starting pipeline...")
            result = api_call(
                "post",
                "/generate-all",
                json={
                    "topic": custom_topic or None,
                    "category": category,
                    "voice": selected_voice,
                    "format": video_format,
                },
            )
            progress.progress(100, text="Complete!")
            if result:
                st.success(f"✅ {result['message']} (Video ID: {result['video_id']})")
                st.balloons()

    st.markdown("---")

    # Step-by-step pipeline
    st.markdown("### 🔧 Step-by-Step Pipeline")
    st.info("Use these for granular control. Each step requires the previous step to complete.")

    # Store current video ID
    if "current_video_id" not in st.session_state:
        st.session_state.current_video_id = None

    cols = st.columns(4)

    # Step 1: Topic
    with cols[0]:
        st.markdown('<span class="step-indicator">1</span> **Topic**', unsafe_allow_html=True)
        if st.button("Generate Topic", use_container_width=True):
            result = api_call(
                "post",
                "/generate-topic",
                json={"topic": custom_topic or None, "category": category},
            )
            if result:
                st.session_state.current_video_id = result["video_id"]
                st.success(f"ID: {result['video_id']}")

    # Step 2: Script
    with cols[1]:
        st.markdown('<span class="step-indicator">2</span> **Script**', unsafe_allow_html=True)
        if st.button("Generate Script", use_container_width=True):
            if st.session_state.current_video_id:
                with st.spinner("Generating script..."):
                    result = api_call(
                        "post",
                        "/generate-script",
                        json={
                            "video_id": st.session_state.current_video_id,
                            "voice": selected_voice,
                        },
                    )
                    if result:
                        st.success("Done!")
            else:
                st.warning("Generate a topic first")

    # Step 3: Audio
    with cols[2]:
        st.markdown('<span class="step-indicator">3</span> **Audio**', unsafe_allow_html=True)
        if st.button("Generate Audio", use_container_width=True):
            if st.session_state.current_video_id:
                with st.spinner("Generating audio..."):
                    result = api_call(
                        "post",
                        "/generate-audio",
                        json={
                            "video_id": st.session_state.current_video_id,
                            "voice": selected_voice,
                        },
                    )
                    if result:
                        st.success("Done!")
            else:
                st.warning("Generate a topic first")

    # Step 4: Images
    with cols[3]:
        st.markdown('<span class="step-indicator">4</span> **Images**', unsafe_allow_html=True)
        if st.button("Generate Images", use_container_width=True):
            if st.session_state.current_video_id:
                with st.spinner("Generating images..."):
                    result = api_call(
                        "post",
                        "/generate-images",
                        json={"video_id": st.session_state.current_video_id},
                    )
                    if result:
                        st.success("Done!")
            else:
                st.warning("Generate a topic first")

    cols2 = st.columns(3)

    # Step 5: Subtitles
    with cols2[0]:
        st.markdown('<span class="step-indicator">5</span> **Subtitles**', unsafe_allow_html=True)
        if st.button("Generate Subtitles", use_container_width=True):
            if st.session_state.current_video_id:
                with st.spinner("Generating subtitles..."):
                    result = api_call(
                        "post",
                        "/generate-subtitles",
                        json={"video_id": st.session_state.current_video_id},
                    )
                    if result:
                        st.success("Done!")
            else:
                st.warning("Generate a topic first")

    # Step 6: Video
    with cols2[1]:
        st.markdown('<span class="step-indicator">6</span> **Video**', unsafe_allow_html=True)
        if st.button("Assemble Video", use_container_width=True):
            if st.session_state.current_video_id:
                with st.spinner("Assembling video (this may take a while)..."):
                    result = api_call(
                        "post",
                        "/generate-video",
                        json={
                            "video_id": st.session_state.current_video_id,
                            "format": video_format,
                        },
                    )
                    if result:
                        st.success("Done!")
            else:
                st.warning("Generate a topic first")

    # Step 7: Thumbnail
    with cols2[2]:
        st.markdown('<span class="step-indicator">7</span> **Thumbnail**', unsafe_allow_html=True)
        if st.button("Generate Thumbnail", use_container_width=True):
            if st.session_state.current_video_id:
                with st.spinner("Generating thumbnail..."):
                    result = api_call(
                        "post",
                        "/generate-thumbnail",
                        json={"video_id": st.session_state.current_video_id},
                    )
                    if result:
                        st.success("Done!")
            else:
                st.warning("Generate a topic first")

    # Show current video status
    if st.session_state.current_video_id:
        st.markdown("---")
        st.markdown(f"### 📌 Current Video (ID: {st.session_state.current_video_id})")
        status_data = api_call("get", f"/status/{st.session_state.current_video_id}")
        if status_data:
            sc1, sc2, sc3 = st.columns(3)
            sc1.markdown(f"**Title:** {status_data.get('title', 'N/A')}")
            sc2.markdown(f"**Status:** {status_data.get('upload_status', 'N/A')}")
            sc3.markdown(f"**Format:** {status_data.get('video_format', 'N/A')}")


# ── Tab 2: Upload ───────────────────────────────────────────────────

with tab2:
    st.markdown("### 📤 Upload to YouTube")

    # Video selector
    upload_vid = st.number_input("Video ID to upload", min_value=1, step=1)

    ucol1, ucol2 = st.columns(2)
    with ucol1:
        privacy = st.selectbox(
            "Privacy Status",
            ["private", "unlisted", "public"],
            format_func=lambda x: {"private": "🔒 Private", "unlisted": "🔗 Unlisted", "public": "🌐 Public"}[x],
        )
    with ucol2:
        schedule_upload = st.checkbox("Schedule publication")
        scheduled_at = None
        if schedule_upload:
            sched_date = st.date_input("Publish date", value=datetime.now() + timedelta(days=1))
            sched_time = st.time_input("Publish time")
            scheduled_at = datetime.combine(sched_date, sched_time).isoformat()

    if st.button("🚀 Upload to YouTube", type="primary", use_container_width=True):
        with st.spinner("Uploading to YouTube..."):
            result = api_call(
                "post",
                "/upload-video",
                json={
                    "video_id": int(upload_vid),
                    "privacy": privacy,
                    "scheduled_at": scheduled_at,
                },
            )
            if result:
                st.success(f"✅ {result['message']}")
                st.balloons()

    # Quick view of ready videos
    st.markdown("---")
    st.markdown("### 📋 Ready for Upload")
    ready_data = api_call("get", "/history")
    if ready_data:
        ready_vids = [v for v in ready_data.get("videos", []) if v.get("upload_status") == "ready"]
        if ready_vids:
            for v in ready_vids:
                with st.container():
                    rc1, rc2, rc3 = st.columns([1, 3, 1])
                    rc1.markdown(f"**ID: {v['id']}**")
                    rc2.markdown(f"_{v.get('title', v.get('topic', 'Untitled'))}_")
                    rc3.markdown(f"🎬 {v.get('video_format', 'horizontal')}")
        else:
            st.info("No videos ready for upload. Generate one first!")


# ── Tab 3: History ──────────────────────────────────────────────────

with tab3:
    st.markdown("### 📜 Video History")

    if st.button("🔄 Refresh", key="refresh_history"):
        st.rerun()

    history_data = api_call("get", "/history")
    if history_data:
        videos = history_data.get("videos", [])
        if videos:
            for v in videos:
                status = v.get("upload_status", "unknown")
                with st.expander(
                    f"#{v['id']} — {v.get('title', v.get('topic', 'Untitled'))} "
                    f"[{status.upper()}]",
                    expanded=False,
                ):
                    hc1, hc2 = st.columns(2)
                    with hc1:
                        st.markdown(f"**Category:** {v.get('category', 'N/A')}")
                        st.markdown(f"**Format:** {v.get('video_format', 'N/A')}")
                        st.markdown(f"**Voice:** {v.get('voice', 'N/A')}")
                        st.markdown(f"**Privacy:** {v.get('privacy_status', 'N/A')}")
                    with hc2:
                        st.markdown(f"**Created:** {v.get('created_at', 'N/A')}")
                        st.markdown(f"**Updated:** {v.get('updated_at', 'N/A')}")
                        if v.get("youtube_url"):
                            st.markdown(f"**YouTube:** [{v['youtube_url']}]({v['youtube_url']})")
                        if v.get("error_message"):
                            st.error(f"Error: {v['error_message']}")

                    if v.get("description"):
                        with st.popover("📝 View Description"):
                            st.markdown(v["description"])
        else:
            st.info("No videos yet. Generate your first video!")


# ── Tab 4: Logs ─────────────────────────────────────────────────────

with tab4:
    st.markdown("### 📋 Application Logs")

    log_file = st.selectbox(
        "Log file",
        ["app.log", "error.log", "upload.log"],
        format_func=lambda x: {
            "app.log": "📝 Application Log",
            "error.log": "❌ Error Log",
            "upload.log": "📤 Upload Log",
        }[x],
    )

    num_lines = st.slider("Lines to display", 10, 200, 50)

    if st.button("🔄 Refresh Logs", key="refresh_logs"):
        pass  # Streamlit reruns automatically

    try:
        import os
        log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", log_file)
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                recent = lines[-num_lines:] if len(lines) > num_lines else lines
                st.code("".join(recent), language="log")
        else:
            st.info(f"Log file not found: {log_path}")
    except Exception as exc:
        st.warning(f"Could not read logs: {exc}")


# ── Footer ───────────────────────────────────────────────────────────

st.markdown("---")
st.markdown(
    "<center><sub>YouTube Automation Studio v1.0 • Powered by AI</sub></center>",
    unsafe_allow_html=True,
)
