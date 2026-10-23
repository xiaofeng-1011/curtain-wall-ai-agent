"""界面主题：浅色冷灰底、蓝色强调、有限玻璃层；内容区域保持高对比度。"""
import streamlit as st


CSS = """
<style>
:root {
  --mw-bg: #edf2f7;
  --mw-surface: rgba(255, 255, 255, .78);
  --mw-solid: #ffffff;
  --mw-ink: #182433;
  --mw-muted: #64748b;
  --mw-line: rgba(120, 145, 170, .24);
  --mw-blue: #245fc7;
  --mw-blue-soft: #e7f0ff;
  --mw-amber: #9a5b00;
  --mw-radius: 14px;
  --mw-control-line: #cbd7e5;
  --mw-selected-bg: #e8f0fd;
  --mw-selected-ink: #194d9f;
  --mw-bubble-bg: #e5eefb;
  --mw-bubble-line: #d7e3f2;
}

html, body, .stMarkdown, .stChatMessage, input, textarea, button {
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "Segoe UI", sans-serif;
  color: var(--mw-ink);
}
[data-testid="stAppViewContainer"] { background: radial-gradient(ellipse at 92% 6%, #dce8f7 0, transparent 40%),
  radial-gradient(ellipse at 15% 95%, #e1eaf0 0, transparent 50%), #eef2f6; }
/* 保留原生顶栏及侧栏开关；内容从固定 3.75rem 顶栏下方开始。 */
[data-testid="stHeader"] { height:3.75rem; background:rgba(238,242,246,.9); }
.block-container { max-width: 960px; padding-top: 4.5rem; padding-bottom: 9rem; }
.stMarkdown p, .stMarkdown li { font-size: 15px; line-height: 1.65; }
.num, td { font-variant-numeric: tabular-nums; }

section[data-testid="stSidebar"] { background: rgba(242, 247, 251, .86); border-right: 1px solid var(--mw-line); }
section[data-testid="stSidebar"] { backdrop-filter:blur(22px) saturate(120%); }
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] { padding:0 1rem 1rem; }
.mw-brand { padding:.2rem .3rem .9rem; }
.mw-brand-name { font-size:17px; font-weight:650; letter-spacing:.015em; }
.mw-brand-sub { color:#526173; font-size:12px; margin-top:5px; }
.st-key-sidebar-history { height:calc(100dvh - 355px)!important; min-height:100px; }
.st-key-sidebar-tools { border-top:1px solid var(--mw-line); padding-top:8px; }
.st-key-sidebar-tools hr { display:none; }
[class*="st-key-history-row-"], [class*="st-key-history-current-"] { min-height:44px; border-radius:10px; padding:0 3px; gap:0; }
[class*="st-key-history-current-"] { background:var(--mw-selected-bg); }
[class*="st-key-history-current-"] button { color:var(--mw-selected-ink); font-weight:600; }
[class*="st-key-history-current-"] button p { color:inherit; font-weight:inherit; }
[class*="st-key-history-row-"]:hover { background:rgba(255,255,255,.75); }
.st-key-sidebar-history .stButton { min-width:0; }
.st-key-sidebar-history .stButton button { height:44px; padding:0 7px; }
.st-key-sidebar-history .stButton button [data-testid="stMarkdownContainer"] { min-width:0; overflow:hidden; }
.st-key-sidebar-history .stButton button p { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.st-key-sidebar-history [data-testid="stPopover"] { flex-shrink:0; }
.st-key-sidebar-history [data-testid="stPopover"] button { width:36px; min-width:36px; height:36px; background:transparent; border:none; }
.st-key-sidebar-history [data-testid="stPopover"] button [data-testid="stMarkdownContainer"],
.st-key-sidebar-history [data-testid="stPopover"] button [data-testid="stIconMaterial"]:last-child:not(:first-child) { display:none; }
.st-key-sidebar-history [data-testid="stPopover"] button [aria-hidden="true"] { display:none; }
/* 保留菜单的可访问名称，只在视觉上收起文字。 */
.st-key-sidebar-history [data-testid="stPopover"] button [data-testid="stMarkdownContainer"] {
  display:block; position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip-path:inset(50%); white-space:nowrap;
}
.mw-brand-mark { display:grid; place-items:center; width:2rem; height:2rem; border-radius:10px;
  color: #fff; background: linear-gradient(145deg, #438ff2, #1e64cb); box-shadow: 0 5px 14px rgba(31,111,235,.2); font-size:1.2rem; }
.mw-brand strong { display:block; font-size:.98rem; letter-spacing:.01em; }
.mw-brand small { display:block; color:var(--mw-muted); font-size:.72rem; margin-top:.12rem; }
.mw-section-label { color:#7b8da1; font-size:.7rem; letter-spacing:.11em; text-transform:uppercase; margin:1rem .45rem .35rem; }
.mw-conv-dot { display:block; padding-top:.45rem; color:var(--mw-blue); font-size:.62rem; }
.mw-conv-dot.muted { color:#a9b7c6; }
section[data-testid="stSidebar"] .stButton button { width:100%; justify-content:flex-start; text-align:left; border:1px solid transparent;
  border-radius:10px; padding:.42rem .55rem; min-height:2.1rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
section[data-testid="stSidebar"] .stButton button:hover { background:rgba(255,255,255,.72); border-color:var(--mw-line); }
section[data-testid="stSidebar"] .stButton button[kind="primary"] { color:#1556ab; background:var(--mw-blue-soft); border-color:rgba(31,111,235,.22); }
section[data-testid="stSidebar"] .st-key-sidebar-history .stButton button,
section[data-testid="stSidebar"] .st-key-sidebar-history .stButton button:hover { background:transparent; border-color:transparent; box-shadow:none; }
section[data-testid="stSidebar"] [data-testid="stPopover"] button { justify-content:center; padding-left:0; padding-right:0; }
/* The root owns the search border; the icon and input share its surface. */
.st-key-conv-search [data-testid="stTextInputRootElement"] {
  min-height:42px; border:2px solid var(--mw-control-line); border-radius:12px; background:#fafcfe; box-shadow:none;
}
.st-key-conv-search [data-testid="stTextInputRootElement"]:focus-within { border-color:var(--mw-blue); }
.st-key-conv-search input, .st-key-conv-search input:focus-visible {
  background:transparent; border:0; border-radius:0; outline:none; box-shadow:none;
}

.st-key-conversation-header { padding:14px 20px; margin-bottom:22px; border:1px solid rgba(255,255,255,.95);
  background:rgba(255,255,255,.78); border-radius:16px; box-shadow:0 8px 28px rgba(31,50,73,.06),0 0 0 1px rgba(100,130,160,.12);
  backdrop-filter:blur(22px) saturate(120%); }
.mw-title { font-size:20px; line-height:28px; font-weight:600; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; padding:3px 0; }
.st-key-conversation-header [data-testid="stHorizontalBlock"] { overflow:visible; min-height:44px; }
.st-key-conversation-header [data-testid="stMarkdown"]:has(.mw-title) { min-height:34px; }
.st-key-conversation-title-row > [data-testid="stElementContainer"]:has(.mw-title) { flex:1 1 240px; min-width:0; }
.mw-subtitle { color:#526173; font-size:13px; line-height:20px; overflow-wrap:anywhere; }
.mw-project-chip { display:inline-flex; align-items:center; background:#eef2f9; color:#245fc7; font-weight:600; font-size:13px;
  padding:3px 10px; border-radius:12px; border:1px solid #d5e0f0; white-space:nowrap; max-width:240px;
  overflow:hidden; text-overflow:ellipsis; flex-shrink:0; }
.mw-status { display:inline-block; margin-left:12px; border-left:1px solid #b9c9dc; padding-left:12px; }
.mw-home { padding:clamp(1.8rem,8vh,6rem) 0 2rem; }
.mw-eyebrow { color:#245fc7; font-size:13px; margin-bottom:18px; letter-spacing:.09em; }
.mw-home h1 { font-size:clamp(28px,3.2vw,40px); line-height:1.48; font-weight:600; margin:0 0 1.15rem; letter-spacing:-.025em; }
.mw-home p.lead { color:#526274; font-size:1rem; margin:0; max-width:36rem; }
.mw-task-hint { margin:1.2rem 0 .55rem; padding:.85rem 1rem; border:1px solid rgba(31,111,235,.2); border-radius:var(--mw-radius);
  background:rgba(231,240,255,.76); }
.mw-task-hint span { display:block; color:var(--mw-blue); font-size:.7rem; letter-spacing:.08em; text-transform:uppercase; }
.mw-task-hint strong { display:block; margin-top:.15rem; font-size:.95rem; }
.mw-task-hint p { margin:.28rem 0 0; color:#526274; font-size:.85rem; }
.mw-scope { color:var(--mw-muted); font-size:.82rem; margin-top:1.35rem; }

[data-testid="stChatMessage"] { padding:.7rem .25rem; background:transparent; }
[data-testid="stChatMessage"]:has([aria-label="Chat message from user"]) { flex-direction:row-reverse; margin-left:auto; width:82%; }
[class*="st-key-answer-bubble-"] {
  background:var(--mw-bubble-bg); border:1px solid var(--mw-bubble-line); border-radius:14px 14px 14px 4px;
  padding:10px 14px; min-width:0; overflow-wrap:anywhere;
}
[aria-label="Chat message from user"] [class*="st-key-answer-bubble-"] { border-radius:14px 14px 4px 14px; }
[class*="st-key-answer-bubble-"] [data-testid="stMarkdownContainer"] { min-width:0; }
[class*="st-key-answer-bubble-"] [data-testid="stMarkdownContainer"] > div:has(table),
[class*="st-key-answer-bubble-"] .katex-display { max-width:100%; overflow-x:auto; }
[data-testid="stChatMessageAvatarCustom"] { width:30px; height:30px; min-width:30px; border-radius:9px; background:#e4edf7; color:#245fc7; }
[data-testid="stChatMessageAvatarUser"], [data-testid="stChatMessageAvatarAssistant"] { width:30px; height:30px; min-width:30px; border-radius:9px; background:#e4edf7; color:#245fc7; }
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p { margin-bottom:.45rem; }
.mw-result { background:rgba(255,255,255,.86); border:1px solid var(--mw-line); border-radius:var(--mw-radius); padding:.55rem .7rem; margin:.65rem 0; box-shadow:0 8px 24px rgba(53,77,102,.06); }
.mw-result { overflow-x:auto; }
.mw-result-identity { padding-top:12px; }
.mw-result-identity strong { font-size:16px; }
.mw-result-identity span { display:block; font-size:12px; color:#526173; margin-top:5px; }
.mw-result-summary { font-size:18px; font-weight:600; line-height:1.7; padding:14px 0 6px; white-space:pre-line; }
[role="dialog"] { background:rgba(250,252,254,.96); border:1px solid white; }
[data-testid="stDialog"] > div:has(> [role="dialog"]) { width:min(920px, calc(100vw - 32px)); }
[role="dialog"]:has([data-testid="stForm"]) { width:100%; max-height:calc(100dvh - 72px); overflow-y:auto; }
[role="dialog"] [data-testid="stTextInputRootElement"],
[role="dialog"] [data-testid="stSelectbox"] [role="combobox"] { border:1px solid #b8c8da; border-radius:8px; background:#fff; }
[role="dialog"] [data-testid="stForm"] { background:#fafcfe; }
.mw-result table { border-collapse:collapse; width:100%; margin:.1rem 0; font-size:.92rem; }
.mw-result th { text-align:right; color:#526274; font-weight:600; border-bottom:1px solid #dfe6ee; padding:.48rem .5rem; }
.mw-result th:first-child, .mw-result td:first-child { text-align:left; }
.mw-result td { text-align:right; padding:.46rem .5rem; border-bottom:1px solid #edf1f5; }
.mw-result td.missing { color:var(--mw-amber); font-size:.84rem; }
.mw-muted { color:var(--mw-muted); font-size:.84rem; }
.mw-stale { color:var(--mw-amber); background:#fff7e8; border:1px solid #f1d7a6; border-radius:10px; padding:.46rem .65rem; font-size:.84rem; margin:.25rem 0 .6rem; }
.mw-tag { display:inline-block; padding:0 .52rem; border-radius:999px; font-size:.73rem; line-height:1.45rem; background:#eef2f6; color:#475569; margin:.22rem .25rem .05rem 0; }
.mw-tag.warn { background:#fff3dc; color:#915600; }
.mw-tag.demo { background:#e7f5ef; color:#246a50; }

[data-testid="stChatInput"] { border:2px solid var(--mw-control-line); border-radius:18px; background:rgba(255,255,255,.84); box-shadow:0 12px 34px rgba(38,66,96,.08); backdrop-filter:blur(22px) saturate(120%); }
/* Streamlit 1.65 adds a second, radius-8 wrapper immediately inside the root. */
[data-testid="stChatInput"] > div,
[data-testid="stChatInput"] div:has(> textarea) { border:0; border-radius:0; background:transparent; box-shadow:none; }
[data-testid="stChatInput"]:focus-within { border-color:var(--mw-blue); }
[data-testid="stChatInput"] textarea,
[data-testid="stChatInput"] textarea:focus-visible { border:0; border-radius:0; background:transparent; box-shadow:none; outline:none; }
[data-testid="stChatInput"] button { min-width:44px; min-height:44px; }
[data-testid="stBottomBlockContainer"] { padding-bottom:1rem; background:transparent; }
[data-testid="stBottom"] { background:transparent; }
[data-testid="stChatInput"] textarea { min-height:68px; max-height:170px; font-size:15px; line-height:1.6; }
.st-key-composer-tools { padding:4px 6px 0; }
.st-key-composer-tools p { font-size:12px; color:#526173; }
button { transition: transform .12s ease, box-shadow .12s ease, background .12s ease; }
button:active { transform:translateY(1px) scale(.99); }
.stButton button[kind="primary"] { background:var(--mw-blue); border-color:var(--mw-blue); }
.stButton button[kind="primary"] p { color:white; }
section[data-testid="stSidebar"] .stButton button[kind="primary"] { background:#245fc7; color:white; }
.stButton button:focus-visible, [data-testid="stPopover"] button:focus-visible,
[data-testid="stChatInput"] button:focus-visible, [data-testid="stMultiSelect"] button:focus-visible {
  outline:2px solid var(--mw-blue); outline-offset:2px;
}

/* Scoped native React Aria multiselects: one shell, flexible tags, native menu. */
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) .react-aria-ComboBox > [role="group"] {
  min-height:44px; padding:6px 8px; gap:6px; border:2px solid var(--mw-control-line); border-radius:12px; background:#fafcfe; box-shadow:none;
}
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) .react-aria-ComboBox > [role="group"]:focus-within { border-color:var(--mw-blue); }
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) [data-testid="stMultiSelectTagsContainer"] { gap:6px; min-width:0; flex-wrap:wrap; }
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) [aria-label="Selected values"] { gap:6px; flex-wrap:wrap; min-width:0; max-width:100%; }
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) [data-tag] {
  min-height:28px; height:auto; max-width:100%; border-radius:8px; background:var(--mw-selected-bg); color:var(--mw-selected-ink); box-shadow:none;
}
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) [data-tag] > span { white-space:normal; overflow-wrap:anywhere; }
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) [data-tag] button { color:var(--mw-selected-ink); flex-shrink:0; }
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) input {
  min-width:3rem; background:transparent; border:0; border-radius:0; outline:none; box-shadow:none;
}
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) [data-tag]:focus-visible { outline:2px solid var(--mw-blue); outline-offset:1px; }
:is([class*="st-key-qa-collections-"], [class*="st-key-qa-std-"], [class*="st-key-qa-ver-"], [class*="st-key-qa-cat-"]) .react-aria-ComboBox > [role="group"] > button { min-width:28px; min-height:28px; flex-shrink:0; }

@media (max-width: 900px) {
  .block-container { padding-left:1rem; padding-right:1rem; }
  .mw-home { padding-top:2.2rem; }
}
@media (max-width:640px) {
  .block-container { padding-top:4.25rem; padding-bottom:11rem; }
  .mw-home { padding-top:1.25rem; }
  .st-key-conversation-header { padding:12px; }
  .mw-title { font-size:18px; }
  .mw-status { margin:5px 0 0; display:block; padding:0; border:0; }
  .st-key-sidebar-history [data-testid="stPopover"] button { height:44px; width:44px; }
  [data-testid="stChatMessage"]:has([aria-label="Chat message from user"]) { width:94%; }
}
@media (prefers-reduced-motion:reduce) { button { transition:none!important; transform:none!important; } }
@supports not (backdrop-filter:blur(1px)) {
  section[data-testid="stSidebar"], .st-key-conversation-header, [data-testid="stChatInput"] { background:#fafcfe; }
}
@media (prefers-reduced-transparency: reduce) {
  section[data-testid="stSidebar"], .st-key-conversation-header, [data-testid="stChatInput"], .mw-result { background:#fff; backdrop-filter:none; }
}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    extra = []
    preferences = st.session_state.get("reading_preferences", {})
    if preferences.get("reduce_transparency"):
        extra.append('section[data-testid="stSidebar"], .st-key-conversation-header, [data-testid="stChatInput"] {background:#fafcfe!important;backdrop-filter:none!important;}')
    if preferences.get("reduce_motion"):
        extra.append('button {transition:none!important;transform:none!important;}')
    if preferences.get("large_text"):
        extra.append('.stMarkdown p,.stMarkdown li {font-size:17px;}')
    if extra:
        st.markdown("<style>" + "".join(extra) + "</style>", unsafe_allow_html=True)

