import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
import streamlit as st

# Paste your OpenRouter API key between the quotes below.
# Do not upload this file with a real key to a public GitHub repository.
OPENROUTER_API_KEY = "PASTE_YOUR_OPENROUTER_API_KEY_HERE"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-4o-mini"
REQUEST_TIMEOUT = 25

# Restrict recommendations to established course providers.
TRUSTED_DOMAINS = {
    "coursera.org", "www.coursera.org",
    "edx.org", "www.edx.org",
    "freecodecamp.org", "www.freecodecamp.org",
    "kaggle.com", "www.kaggle.com",
    "learn.microsoft.com",
    "skillsbuild.org", "www.skillsbuild.org",
    "developers.google.com",
    "aws.amazon.com",
    "academy.hubspot.com",
    "classcentral.com", "www.classcentral.com",
    "ocw.mit.edu",
    "cs50.harvard.edu",
    "learn.nvidia.com",
    "huggingface.co",
    "python.org", "www.python.org",
    "Cisco.com".lower(), "www.netacad.com",
    "netacad.com",
    "saylor.org", "www.saylor.org",
    "alison.com", "www.alison.com",
}

st.set_page_config(
    page_title="AI Course Finder",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .main { background: #f7f9fc; }
    .hero {
        padding: 1.4rem 1.6rem; border-radius: 18px;
        background: linear-gradient(120deg, #102a43, #245b83);
        color: white; margin-bottom: 1rem;
    }
    .course-card {
        background: white; padding: 1.1rem 1.2rem; border-radius: 14px;
        border: 1px solid #e4eaf1; margin-bottom: .8rem;
    }
    .muted { color: #536579; font-size: .93rem; }
</style>
""", unsafe_allow_html=True)

def domain_is_trusted(url: str) -> bool:
    """Allow only HTTPS links on a known learning provider domain."""
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(
            host == d or host.endswith("." + d)
            for d in TRUSTED_DOMAINS
        )
    except Exception:
        return False


def check_url(url: str) -> bool:
    """Quick reachability check. Some providers block automated requests."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CourseFinder/1.0)"}
    try:
        response = requests.get(
            url, headers=headers, timeout=4, allow_redirects=True, stream=True
        )
        status = response.status_code
        response.close()
        # 403/405 can be caused by anti-bot rules, not necessarily a dead page.
        return status < 400 or status in (403, 405, 429)
    except requests.RequestException:
        return False


def request_courses(domain: str, level: str, goal: str):
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY == "PASTE_YOUR_OPENROUTER_API_KEY_HERE":
        raise ValueError(
            "Add your OpenRouter API key to OPENROUTER_API_KEY near the top of app.py."
        )

    system_prompt = """
You are a careful course research assistant. Recommend real, reputable learning
courses that are FREE to access (free learning content; certificates may cost extra).
Return exactly one JSON object with a 'courses' array containing exactly 5 objects.
Each object must have these keys:
title, provider, level, description, free_details, url.
Use direct official course/enrollment URLs, not search-result links. Prefer well-known
official providers such as freeCodeCamp, Kaggle Learn, Microsoft Learn, Google
Developers, Harvard CS50, MIT OpenCourseWare, Cisco Networking Academy, Saylor,
Hugging Face, and reputable Coursera/edX course pages where free audit/content is
available. Do not claim a paid certificate is free. Do not invent URLs; if uncertain,
choose a different course with a known official page. Match the requested domain and
level. No markdown fences, no text outside JSON.
"""
    user_prompt = f"""
Find the top 5 worthwhile free courses for this domain: {domain}
Learner level: {level}
Learning goal (if any): {goal or "General learning and practical skills"}
Return JSON only. Include a short, useful description and explain what is free.
"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8501",
        "X-OpenRouter-Title": "AI Course Finder",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.15,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }

    try:
        response = requests.post(
            OPENROUTER_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
        )
    except requests.Timeout as exc:
        raise RuntimeError("The AI request timed out. Please try again.") from exc
    except requests.RequestException as exc:
        raise RuntimeError("Could not connect to OpenRouter. Check your internet connection.") from exc

    if response.status_code in (401, 403):
        raise ValueError("OpenRouter rejected the API key. Check the key in app.py.")
    if response.status_code == 429:
        raise RuntimeError("OpenRouter rate limit reached. Wait briefly and try again.")
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter request failed (HTTP {response.status_code}). Try again.")

    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("The AI returned an unreadable response. Please retry.") from exc

    raw_courses = data.get("courses", [])
    if not isinstance(raw_courses, list):
        raise RuntimeError("The AI response had an unexpected format. Please retry.")

    cleaned = []
    seen = set()
    for item in raw_courses:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        provider = str(item.get("provider", "")).strip()
        url = str(item.get("url", "")).strip()
        if not title or not provider or not url:
            continue
        if not domain_is_trusted(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        cleaned.append({
            "title": title[:180],
            "provider": provider[:100],
            "level": str(item.get("level", level)).strip()[:50],
            "description": str(item.get("description", "")).strip()[:500],
            "free_details": str(item.get("free_details", "Free learning access; check provider terms.")).strip()[:300],
            "url": url,
        })

    # Check links concurrently to keep the dashboard responsive.
    reachable = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(check_url, course["url"]): course for course in cleaned}
        for future in as_completed(futures):
            course = futures[future]
            try:
                if future.result():
                    reachable.append(course)
            except Exception:
                pass

    # Preserve model ranking order after concurrent checks.
    reachable_urls = {c["url"] for c in reachable}
    result = [c for c in cleaned if c["url"] in reachable_urls][:5]

    if len(result) < 5:
        raise RuntimeError(
            f"Only {len(result)} suitable course links passed the checks. "
            "Try a broader domain (for example, 'Python programming') or search again."
        )
    return result


# Dashboard
with st.sidebar:
    st.title("🎓 Course Finder")
    st.caption("Discover useful free learning resources.")
    st.markdown("---")
    st.markdown("**How it works**")
    st.write("1. Enter a learning domain.")
    st.write("2. Choose your level.")
    st.write("3. Get five course links.")
    st.info("Free course access does not always include a free certificate.")

st.markdown("""
<div class="hero">
  <h1 style="margin:0">AI Course Finder</h1>
  <p style="margin:.45rem 0 0 0">Find five useful free courses for your next skill.</p>
</div>
""", unsafe_allow_html=True)

left, right = st.columns([2, 1])
with left:
    domain = st.text_input(
        "Enter a domain",
        placeholder="e.g. Artificial Intelligence, Machine Learning, Python, Cybersecurity",
        help="Use a specific topic for more relevant recommendations.",
    )
with right:
    level = st.selectbox("Your level", ["Beginner", "Intermediate", "Advanced", "All levels"])

goal = st.text_input(
    "Learning goal (optional)",
    placeholder="e.g. build projects, prepare for a job, learn fundamentals",
)

search = st.button("🔎 Find 5 free courses", type="primary", use_container_width=True)

if search:
    if not domain.strip():
        st.warning("Please enter a domain first.")
    elif len(domain.strip()) > 100:
        st.warning("Please keep the domain under 100 characters.")
    else:
        with st.spinner("Finding and checking course links..."):
            try:
                courses = request_courses(domain.strip(), level, goal.strip())
                st.session_state["courses"] = courses
                st.session_state["last_domain"] = domain.strip()
            except (ValueError, RuntimeError) as exc:
                st.error(str(exc))
            except Exception:
                st.error("Something unexpected happened. Please try again.")

courses = st.session_state.get("courses", [])
if courses:
    st.markdown(f"### Top 5 free courses for **{st.session_state.get('last_domain', 'your domain')}**")
    st.caption("Links are restricted to established learning-provider domains and checked for reachability. Always confirm current pricing on the provider's page.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Courses found", len(courses))
    c2.metric("Access", "Free learning")
    c3.metric("Level", level)

    for index, course in enumerate(courses, start=1):
        st.markdown('<div class="course-card">', unsafe_allow_html=True)
        st.markdown(f"#### {index}. {course['title']}")
        st.markdown(f"**Provider:** {course['provider']}  ·  **Level:** {course['level']}")
        st.write(course["description"] or "A course selected for relevance to your chosen domain.")
        st.markdown(f"**Free access:** {course['free_details']}")
        st.markdown(f"[Open course / apply ↗]({course['url']})")
        st.markdown("</div>", unsafe_allow_html=True)

    export_data = json.dumps(courses, indent=2, ensure_ascii=False)
    st.download_button(
        "⬇️ Download course list (JSON)",
        data=export_data,
        file_name="free_courses.json",
        mime="application/json",
    )
else:
    st.markdown("### Try a domain")
    st.write("AI · Machine Learning · Python · Data Science · Cybersecurity · Web Development")
    st.caption("Enter your topic above and select **Find 5 free courses** to begin.")
