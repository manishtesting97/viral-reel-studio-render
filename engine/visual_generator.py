"""Universal deterministic visual scene generator, prompt compiler, and validator."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

import asset_library
from motion_choreography import compile_scene_storyboard, validate_artwork
from production_plan import BRAND_DIR, STYLE_LIB, digest, load, profiles, save, write
import production_render
import sponsorship

CONFIG_FILE = "visual-config.json"

SUPPORTED_MODES = ("auto", "custom", "hybrid")
VALID_HTML_COMPONENTS = (
    "comparison_split",
    "editor_history",
    "offline_workflow",
    "three_workstation",
    "three_node_offline",
    "history_snapshots",
    "platform_browser",
    "collaboration_network",
    "terminal_pipeline",
    "folder_workspace",
    "search_serp",
    "process_timeline",
    "diagram_explainer",
    "metric_showcase",
    "smartphone_ui",
    "media_spotlight",
    "creator_cta_outro",
)

FORBIDDEN_EXT_URLS = re.compile(r"""(?:src|href|action|data)\s*=\s*["']https?://|url\(\s*["']?https?://|fetch\(\s*["']https?://""", re.I)
FORBIDDEN_JS = re.compile(r"""\b(?:eval|Function)\s*\(|new\s+Function\b|XMLHttpRequest\b|WebSocket\b|\bimport\s*\(\s*["']https?://""", re.I)
SECRET_LEAK = re.compile(r"""(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)""")

COMMON_CSS = """/* Auto-generated universal editorial scene stylesheet inheriting brand and style tokens. */
#stage {
  --vrs-header-top: 236px;
  --vrs-header-left: 68px;
  --vrs-profile-gap: 90px;
  --vrs-visual-gap: 90px;
}
#profile-header {
  box-shadow: none;
  gap: 14px;
}
#profile-header img {
  box-shadow: 0 8px 18px rgba(24, 20, 18, 0.14);
  border: 2px solid rgba(255, 255, 255, 0.92);
}
#profile-header strong {
  font-size: 27px;
  letter-spacing: -0.01em;
}
#profile-header small {
  font-size: 18px;
  opacity: 0.78;
}
.vrs-auto-scene {
  position: absolute;
  inset: 0;
  overflow: hidden;
  font-family: var(--brand-font, "Poppins", "Hind", sans-serif);
  color: var(--brand-ink, #181412);
  background: var(--style-ground, #faf5f0);
}
.vrs-auto-scene * { box-sizing: border-box; }
.vrs-auto-bg {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background-image:
    radial-gradient(circle at 88% 10%, var(--style-glow-a, rgba(234, 88, 12, 0.11)) 0%, transparent 40%),
    radial-gradient(circle at 12% 84%, var(--style-glow-b, rgba(251, 146, 60, 0.10)) 0%, transparent 44%),
    radial-gradient(var(--style-grid-dot, rgba(180, 152, 132, 0.22)) 1.5px, transparent 1.5px);
  background-size: 100% 100%, 100% 100%, 28px 28px;
}
.vrs-progress-rail {
  position: absolute;
  top: 0;
  left: 0;
  width: 1080px;
  height: 6px;
  background: rgba(24, 20, 18, 0.06);
  z-index: 40;
}
.vrs-progress-fill {
  width: 100%;
  height: 100%;
  transform-origin: left center;
  background: linear-gradient(90deg, var(--brand-accent, #ea580c), var(--brand-highlight, #ffd400));
}
.vrs-meta-badge {
  position: absolute;
  top: calc(var(--vrs-header-top, 236px) + 6px);
  right: var(--vrs-header-left, 68px);
  z-index: 30;
  min-height: 46px;
  max-width: 440px;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 8px 18px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.94);
  border: 1.5px solid var(--style-border, #e5dbd0);
  box-shadow: 0 8px 20px rgba(24, 20, 18, 0.06);
  font-size: 21px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--brand-ink, #181412);
}
.vrs-meta-badge .k8s-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--brand-accent, #ea580c);
  box-shadow: 0 0 0 4px rgba(234, 88, 12, 0.14);
  flex: none;
}
.vrs-meta-badge .vrs-badge-tag {
  background: var(--brand-highlight, #ffd400);
  color: #181412;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 18px;
  font-weight: 800;
}
.vrs-auto-layout {
  position: absolute;
  top: calc(var(--vrs-profile-bottom, var(--vrs-header-top)) + var(--vrs-profile-gap));
  left: 68px;
  right: 68px;
  bottom: 550px;
  display: flex;
  flex-direction: column;
  gap: var(--vrs-visual-gap);
}
.vrs-artwork-layout {
  bottom: max(550px, var(--vrs-caption-clearance, 550px));
  gap: min(64px, var(--vrs-visual-gap));
}
.vrs-auto-heading {
  flex: none;
  margin: 0;
  font-family: var(--vrs-heading-font, var(--brand-font, "Poppins", "Hind", sans-serif));
  font-size: var(--vrs-heading-size, 66px);
  font-weight: 700;
  line-height: 1.08;
  letter-spacing: -0.025em;
  text-align: center;
  color: var(--vrs-heading-color, var(--brand-ink, #181412));
}
.vrs-auto-heading em {
  font-family: var(--vrs-serif-font, "Noto Serif", Georgia, serif);
  font-style: italic;
  font-weight: 700;
  color: #d9480f;
  letter-spacing: -0.015em;
}
.vrs-auto-stage {
  position: relative;
  height: 920px;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.vrs-emblem-card {
  position: relative;
  background: var(--style-surface, #ffffff);
  border: 1.5px solid var(--style-border, #e8dfd6);
  border-radius: 28px;
  box-shadow: 0 24px 52px rgba(48, 28, 16, 0.10), 0 4px 14px rgba(48, 28, 16, 0.05);
  padding: 28px 24px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: space-between;
  text-align: center;
  gap: 14px;
}
.vrs-click-ring {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 64px;
  height: 64px;
  margin: -32px 0 0 -32px;
  border-radius: 50%;
  border: 4px solid #10b981;
  pointer-events: none;
  opacity: 0;
}
.vrs-pkt {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-radius: 999px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 19px;
  font-weight: 800;
  color: #ffffff;
  background: linear-gradient(135deg, #ea580c, #d9480f);
  box-shadow: 0 10px 24px rgba(234, 88, 12, 0.34);
  pointer-events: none;
}
.vrs-card,
.vrs-window {
  position: relative;
  background: var(--style-surface, #ffffff);
  border: 1.5px solid var(--style-border, #e5dbd0);
  border-radius: 22px;
  box-shadow: 0 18px 40px rgba(24, 20, 18, 0.08), 0 3px 10px rgba(24, 20, 18, 0.04);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.vrs-window--dark {
  background: linear-gradient(165deg, #1b1917 0%, #11100f 100%);
  border-color: #322d29;
  color: #fafaf9;
}
.vrs-win-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 13px 20px;
  border-bottom: 1.5px solid rgba(120, 113, 108, 0.16);
  background: rgba(24, 20, 18, 0.025);
}
.vrs-window--dark .vrs-win-head {
  background: rgba(255, 255, 255, 0.03);
  border-bottom-color: #292524;
}
.vrs-win-dots { display: flex; gap: 8px; align-items: center; flex: none; }
.vrs-win-dots span { width: 12px; height: 12px; border-radius: 50%; display: block; }
.vrs-browser-url {
  flex: 1;
  background: rgba(24, 20, 18, 0.045);
  border: 1px solid rgba(120, 113, 108, 0.18);
  border-radius: 999px;
  padding: 6px 16px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 19px;
  font-weight: 600;
  color: #57534e;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.vrs-window--dark .vrs-browser-url {
  background: #0c0a09;
  border-color: #322d29;
  color: #a8a29e;
}
.vrs-mono {
  font-family: "JetBrains Mono", ui-monospace, monospace;
}
.vrs-serif-em {
  font-family: "Noto Serif", "Liberation Serif", Georgia, serif;
  font-style: italic;
}
.vrs-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-radius: 999px;
  font-size: 21px;
  font-weight: 700;
  white-space: nowrap;
  background: var(--style-surface-muted, #f6efe8);
  color: var(--brand-ink, #181412);
  border: 1.5px solid var(--style-border, #e5dbd0);
}
.vrs-pill--accent { background: var(--brand-accent, #ea580c); color: #ffffff; border-color: transparent; }
.vrs-pill--dark { background: #1c1917; color: #fafaf9; border-color: #3f3a36; }
.vrs-pill--success { background: #dcfce7; color: #065f46; border-color: #86efac; }
.vrs-pill--danger { background: #fee2e2; color: #991b1b; border-color: #fca5a5; }
.vrs-pill--indigo { background: #eef2ff; color: #3730a3; border-color: #c7d2fe; }
.vrs-pill--warm { background: #fff7ed; color: #9a3412; border-color: #fed7aa; }
.vrs-pill-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.vrs-rubber-stamp {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 18px;
  border-radius: 12px;
  border: 3.5px solid currentColor;
  box-shadow: inset 0 0 0 2px rgba(255, 255, 255, 0.25), 0 10px 22px rgba(24, 20, 18, 0.14);
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  transform: rotate(-7deg);
}
.vrs-cursor-ptr {
  position: absolute;
  top: 0;
  left: 0;
  width: 40px;
  height: 40px;
  z-index: 32;
  pointer-events: none;
  filter: drop-shadow(0 8px 14px rgba(24, 20, 18, 0.28));
}
.vrs-type-clip {
  display: inline-block;
  overflow: hidden;
  white-space: nowrap;
  border-right: 3px solid var(--brand-accent, #ea580c);
  padding-right: 4px;
}
.vrs-code-pane {
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 20px;
  line-height: 1.45;
  background: #fcfaf7;
}
.vrs-code-line {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 8px 14px;
  border-radius: 10px;
  color: #3f3a36;
}
.vrs-code-ln {
  width: 28px;
  color: #a8a29e;
  font-size: 17px;
  text-align: right;
  flex: none;
}
.vrs-diff-line {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-radius: 11px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 20px;
  font-weight: 600;
}
.vrs-diff-add { background: #ecfdf5; color: #065f46; border-left: 4px solid #10b981; }
.vrs-diff-del { background: #fef2f2; color: #991b1b; border-left: 4px solid #ef4444; }
.vrs-window--dark .vrs-diff-add { background: rgba(16, 185, 129, 0.16); color: #34d399; }
.vrs-window--dark .vrs-diff-del { background: rgba(239, 68, 68, 0.18); color: #f87171; }
.vrs-clash-bar {
  position: relative;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.vrs-clash-line {
  position: absolute;
  left: 36px;
  right: 36px;
  height: 3px;
  border-radius: 3px;
  background: linear-gradient(90deg, transparent, var(--brand-accent, #ea580c), var(--brand-highlight, #ffd400), transparent);
}
.vrs-op-circle {
  position: relative;
  z-index: 4;
  width: 70px;
  height: 70px;
  border-radius: 50%;
  background: var(--brand-highlight, #ffd400);
  color: #181412;
  font-size: 38px;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 4px solid #ffffff;
  box-shadow: 0 12px 26px rgba(234, 179, 8, 0.34);
}
.vrs-three-viewport {
  position: relative;
  width: 100%;
  height: 676px;
  border-radius: 24px;
  overflow: hidden;
  background: radial-gradient(circle at 52% 40%, #1e293b 0%, #0f172a 68%, #090d1a 100%);
  border: 2px solid #2b3954;
  box-shadow: 0 24px 52px rgba(15, 23, 42, 0.20);
}
.vrs-three-viewport canvas {
  display: block;
  width: 100%;
  height: 100%;
}
.vrs-three-hud-top {
  position: absolute;
  top: 18px;
  left: 20px;
  right: 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  pointer-events: none;
  z-index: 6;
}
.vrs-three-hud-bot {
  position: absolute;
  left: 20px;
  right: 20px;
  bottom: 18px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  pointer-events: none;
  z-index: 6;
}
.vrs-tele-dock {
  flex: 1;
  background: var(--style-surface, #ffffff);
  border: 1.5px solid var(--style-border, #e5dbd0);
  border-radius: 22px;
  padding: 18px 24px;
  box-shadow: 0 16px 34px rgba(24, 20, 18, 0.08);
  display: grid;
  grid-template-columns: 1fr auto 1fr auto 1fr;
  align-items: center;
  gap: 12px;
}
.vrs-tele-node {
  background: var(--style-surface-muted, #f6efe8);
  border: 1.5px solid var(--style-border, #e5dbd0);
  border-radius: 16px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.vrs-tele-node strong { font-size: 22px; line-height: 1.15; }
.vrs-tele-node span { font-size: 18px; color: var(--style-muted-ink, #57534e); font-weight: 600; }
.vrs-toggle-track {
  display: inline-flex;
  align-items: center;
  gap: 14px;
  padding: 12px 22px;
  border-radius: 999px;
  background: #ffffff;
  border: 1.5px solid #e5dbd0;
  box-shadow: 0 12px 26px rgba(24, 20, 18, 0.07);
}
.vrs-toggle-switch {
  position: relative;
  width: 78px;
  height: 42px;
  border-radius: 999px;
  background: #10b981;
  padding: 5px;
}
.vrs-toggle-knob {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: #ffffff;
  box-shadow: 0 3px 8px rgba(0, 0, 0, 0.2);
  transform: translateX(36px);
}
.vrs-cta-card {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 18px;
  text-align: center;
  background: rgba(255, 255, 255, 0.94);
  border: 1.5px solid var(--style-border, #e5dbd0);
  border-radius: 28px;
  padding: 36px;
  box-shadow: 0 22px 50px rgba(24, 20, 18, 0.08);
}
.vrs-portrait-ring {
  padding: 10px;
  border-radius: 50%;
  background: conic-gradient(var(--brand-highlight, #ffd400), #fff3a1, var(--brand-accent, #ea580c), var(--brand-highlight, #ffd400));
  box-shadow: 0 18px 44px rgba(234, 88, 12, 0.24);
}
.vrs-portrait-ring img {
  display: block;
  width: 210px;
  height: 210px;
  border-radius: 50%;
  object-fit: cover;
}
.vrs-cta-pill {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  background: var(--brand-highlight, #ffd400);
  color: #181412;
  font-size: 34px;
  font-weight: 800;
  padding: 16px 38px;
  border-radius: 999px;
  box-shadow: 0 14px 30px rgba(234, 179, 8, 0.30);
}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config_path(project: Path) -> Path:
    return project / CONFIG_FILE


def load_visual_config(project: Path) -> dict:
    path = _config_path(project)
    if path.is_file():
        data = load(path)
        if isinstance(data, dict):
            return data
    return {"version": 1, "mode": "auto", "scenes": {}}


def _icon_svg(kind: str, accent: str = "#ea580c", secondary: str = "#4f46e5") -> str:
    icons = {
        "branch": (
            f'<svg width="84" height="84" viewBox="0 0 84 84" fill="none">'
            f'<circle cx="24" cy="20" r="9" fill="{accent}"/>'
            f'<circle cx="24" cy="64" r="9" fill="{accent}"/>'
            f'<circle cx="60" cy="40" r="9" fill="{secondary}"/>'
            f'<path d="M24 29V55M24 29C24 41 60 31 60 49" stroke="{accent}" stroke-width="6" stroke-linecap="round"/>'
            f'</svg>'
        ),
        "cloud": (
            f'<svg width="84" height="84" viewBox="0 0 84 84" fill="none">'
            f'<path d="M24 58H61C68 58 73 53 73 46C73 40 69 35 63 34C61 23 52 16 41 16C29 16 20 25 20 37C14 38 11 43 11 49C11 54 16 58 24 58Z" fill="{secondary}" fill-opacity="0.18" stroke="{secondary}" stroke-width="4.5" stroke-linejoin="round"/>'
            f'<rect x="28" y="42" width="28" height="22" rx="6" fill="#0f172a" stroke="#94a3b8" stroke-width="3"/>'
            f'</svg>'
        ),
        "laptop": (
            f'<svg width="84" height="84" viewBox="0 0 84 84" fill="none">'
            f'<rect x="16" y="18" width="52" height="36" rx="6" fill="#0f172a" stroke="{accent}" stroke-width="4"/>'
            f'<rect x="23" y="25" width="38" height="22" rx="3" fill="#1e293b"/>'
            f'<path d="M8 60H76L70 68H14L8 60Z" fill="{secondary}"/>'
            f'</svg>'
        ),
        "wifi_off": (
            f'<svg width="78" height="78" viewBox="0 0 78 78" fill="none">'
            f'<path d="M12 28C28 15 50 15 66 28M22 40C32 32 46 32 56 40M32 52C36 49 42 49 46 52" stroke="#94a3b8" stroke-width="5" stroke-linecap="round"/>'
            f'<circle cx="39" cy="61" r="4.5" fill="#94a3b8"/>'
            f'<path d="M16 16L62 62" stroke="#dc2626" stroke-width="6" stroke-linecap="round"/>'
            f'</svg>'
        ),
        "wifi_on": (
            f'<svg width="78" height="78" viewBox="0 0 78 78" fill="none">'
            f'<path d="M12 28C28 15 50 15 66 28M22 40C32 32 46 32 56 40M32 52C36 49 42 49 46 52" stroke="#10b981" stroke-width="5" stroke-linecap="round"/>'
            f'<circle cx="39" cy="61" r="4.5" fill="#10b981"/>'
            f'</svg>'
        ),
        "leaf": (
            f'<svg width="84" height="84" viewBox="0 0 84 84" fill="none">'
            f'<path d="M18 66C18 35 42 18 68 18C68 44 51 66 18 66Z" fill="#10b981" fill-opacity="0.20" stroke="#059669" stroke-width="4.5"/>'
            f'<path d="M18 66L54 32" stroke="#059669" stroke-width="4.5" stroke-linecap="round"/>'
            f'</svg>'
        ),
        "chart": (
            f'<svg width="84" height="84" viewBox="0 0 84 84" fill="none">'
            f'<rect x="14" y="44" width="14" height="24" rx="4" fill="{accent}"/>'
            f'<rect x="35" y="30" width="14" height="38" rx="4" fill="{secondary}"/>'
            f'<rect x="56" y="18" width="14" height="50" rx="4" fill="#10b981"/>'
            f'</svg>'
        ),
        "spark": (
            f'<svg width="84" height="84" viewBox="0 0 84 84" fill="none">'
            f'<path d="M42 12L49 35L72 42L49 49L42 72L35 49L12 42L35 35L42 12Z" fill="{accent}" fill-opacity="0.22" stroke="{accent}" stroke-width="4.5" stroke-linejoin="round"/>'
            f'</svg>'
        ),
    }
    return icons.get(kind, icons["spark"])


def _subject_emblem_svg(subject: str, accent: str = "#ea580c", secondary: str = "#4350d8") -> str:
    """Return a clean high-contrast 124px emblem SVG derived from topic entity signals."""
    low = (subject or "").lower()
    if "github" in low:
        return (
            '<svg width="124" height="124" viewBox="0 0 124 124" fill="none">'
            '<circle cx="62" cy="62" r="56" fill="#181412"/>'
            '<path d="M62 24C41 24 24 41 24 62c0 16.8 10.9 31 26 36 1.9.3 2.6-.8 2.6-1.8v-6.6c-10.6 2.3-12.8-5.1-12.8-5.1-1.7-4.4-4.2-5.6-4.2-5.6-3.4-2.3.3-2.3.3-2.3 3.8.3 5.8 3.9 5.8 3.9 3.4 5.8 8.9 4.1 11.1 3.1.3-2.4 1.3-4.1 2.4-5-8.5-1-17.4-4.2-17.4-18.8 0-4.2 1.5-7.6 3.9-10.3-.4-1-1.7-4.8.4-10 0 0 3.2-1 10.5 3.9 3-.8 6.3-1.3 9.5-1.3 3.2 0 6.5.4 9.5 1.3 7.3-4.9 10.5-3.9 10.5-3.9 2.1 5.2.8 9 .4 10 2.4 2.7 3.9 6.1 3.9 10.3 0 14.6-8.9 17.8-17.4 18.8 1.4 1.2 2.6 3.5 2.6 7.1v10.5c0 1 .7 2.2 2.6 1.8 15.1-5 26-19.2 26-36 0-21-17-38-38-38z" fill="#ffffff"/>'
            '</svg>'
        )
    if "git" in low:
        return (
            '<svg width="124" height="124" viewBox="0 0 124 124" fill="none">'
            '<rect x="20" y="20" width="84" height="84" rx="20" transform="rotate(45 62 62)" fill="#f05032"/>'
            '<path d="M51 34L82 65" stroke="#ffffff" stroke-width="6.5" stroke-linecap="round"/>'
            '<path d="M62 45V78" stroke="#ffffff" stroke-width="6.5" stroke-linecap="round"/>'
            '<circle cx="62" cy="45" r="7.5" fill="#ffffff"/>'
            '<circle cx="62" cy="78" r="7.5" fill="#ffffff"/>'
            '<circle cx="80" cy="63" r="7.5" fill="#ffffff"/>'
            '</svg>'
        )
    if any(k in low for k in ("cloud", "platform", "host", "server", "hub", "remote")):
        return (
            f'<svg width="124" height="124" viewBox="0 0 124 124" fill="none">'
            f'<circle cx="62" cy="62" r="56" fill="#eef2ff" stroke="{secondary}" stroke-width="3"/>'
            f'<path d="M36 78H86C94 78 100 72 100 64C100 56 95 50 87 49C84 35 72 26 58 26C43 26 32 37 32 52C25 54 21 60 21 67C21 73 27 78 36 78Z" fill="{secondary}"/>'
            f'<path d="M62 48V70M52 57L62 47L72 57" stroke="#ffffff" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg>'
        )
    if any(k in low for k in ("protein", "food", "diet", "health", "leaf", "plant")):
        return _icon_svg("leaf", accent, secondary).replace('width="84" height="84"', 'width="124" height="124"')
    initials = "".join(w[0].upper() for w in re.findall(r"[A-Za-z0-9]+", subject or "A")[:2]) or "UI"
    return (
        f'<svg width="124" height="124" viewBox="0 0 124 124" fill="none">'
        f'<circle cx="62" cy="62" r="54" fill="#fff7ed" stroke="{accent}" stroke-width="4"/>'
        f'<text x="62" y="75" text-anchor="middle" font-family="Poppins,sans-serif" font-size="40" font-weight="800" fill="{accent}">{escape(initials)}</text>'
        '</svg>'
    )


def _extract_quoted_labels(text: str, limit: int = 4) -> list[str]:
    found: list[str] = []
    for m in re.finditer(r"""(?:['"`]([A-Za-z0-9._+\- /]{2,34})['"`]|\b([A-Z][A-Z0-9 +\-]{2,26})\b)""", text or ""):
        cand = (m.group(1) or m.group(2) or "").strip(" .,:;")
        if cand and cand.lower() not in {"id", "html", "css", "js", "api", "url", "ui"} and cand not in found:
            found.append(cand)
        if len(found) >= limit:
            break
    return found


def _short_label(text: str, max_len: int = 20) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if len(clean) <= max_len:
        return clean
    if "+" in clean:
        head = clean.split("+")[0].strip()
        if 3 <= len(head) <= max_len:
            return head
    cut = clean[:max_len].rsplit(" ", 1)[0].strip(" +/,:-")
    return cut if len(cut) >= 3 else clean[:max_len].strip()


def _extract_commands(scene: dict) -> list[str]:
    blob = f"{scene.get('visual', {}).get('description', '')} {scene.get('narration', '')}"
    cmds = []
    for m in re.finditer(r"`([^`\n]{3,42})`", blob):
        val = m.group(1).strip()
        if any(tok in val for tok in (" ", "-", ".")) and val not in cmds:
            cmds.append(val)
    if not cmds:
        for kw in ("git add", "git commit", "git push", "git status", "git merge", "npm ", "python ", "docker ", "kubectl "):
            idx = blob.lower().find(kw)
            if idx >= 0:
                window = blob[idx:idx + 64]
                qmatch = re.match(r'^([^\n,"]*?"[^"\n]+")', window)
                if qmatch:
                    frag = qmatch.group(1).strip()
                else:
                    frag = re.split(r"[,;\n]|\.(?:\s|$)|\s+(?:and|as|for|with|on|by|in|to|from|par|se|ke|aur|karo|karke|karte|hota)\b", window, maxsplit=1, flags=re.IGNORECASE)[0].strip()
                if frag and frag not in cmds:
                    cmds.append(frag[:40])
    # Derive clean step labels from narration if no literal commands exist
    if not cmds:
        words = [w.strip(".,?!;:") for w in scene.get("heading", "").split()]
        cmds = [" ".join(words[:4]) or scene["id"]]
    return cmds[:4]


def _extract_bullets(scene: dict, limit: int = 3) -> list[str]:
    narration = (scene.get("narration") or "").strip()
    parts = [p.strip() for p in re.split(r"[.?!,;]+", narration) if len(p.strip()) >= 8]
    if not parts:
        parts = [scene.get("heading", scene["id"])]
    substantive = [p for p in parts if len(p.split()) >= 4]
    chosen = substantive if len(substantive) >= limit else parts
    out = []
    for p in chosen[:limit]:
        words = p.split()
        out.append(" ".join(words[:7]) + ("…" if len(words) > 7 else ""))
    return out


def _pick_summary_labels(text: str) -> tuple[str, str]:
    labels = [x for x in _extract_quoted_labels(text, 6) if len(x) >= 3]
    if len(labels) >= 2:
        return labels[0], labels[1]
    return ("Primary Concept", "Applied Result")


def _domain_icon(text: str) -> str:
    low = text.lower()
    if any(k in low for k in ("leaf", "plant", "protein", "nutrition", "food", "diet", "water", "health", "vitamin", "meal", "recipe")):
        return "leaf"
    if any(k in low for k in ("cloud", "server", "host", "platform", "api", "online", "web", "repo", "database")):
        return "cloud"
    if any(k in low for k in ("git", "branch", "merge", "version", "commit", "diff")):
        return "branch"
    if any(k in low for k in ("laptop", "computer", "offline", "local", "code", "editor")):
        return "laptop"
    if any(k in low for k in ("percent", "%", "metric", "growth", "revenue", "budget", "calorie", "chart", "score", "rate")):
        return "chart"
    return "spark"


def select_component(
    scene: dict,
    brief: dict,
    index: int,
    total: int,
    used_recently: list[str],
    assignment: dict | None = None,
) -> str:
    """Select an appropriate reusable editorial visual component deterministically from semantic signals."""
    sid = scene["id"].lower()
    desc = (scene.get("visual", {}).get("description") or "").lower()
    heading = (scene.get("heading") or "").lower()
    narr = (scene.get("narration") or "").lower()
    vtype = scene.get("visual", {}).get("type", "html")
    niche = (brief.get("niche") or "").lower()
    blob = f"{sid} {heading} {desc} {narr}"

    if assignment and assignment.get("layers") and assignment.get("mode") in {"hybrid", "image", "video"}:
        if "artwork" in scene.get("storyboard", {}) and assignment["mode"] == "hybrid":
            return "semantic-artwork"
        return "media_spotlight"
    if "artwork" in scene.get("storyboard", {}):
        return "semantic-artwork"

    if index + 1 == total and any(k in blob for k in ("follow", "cta", "subscribe", "outro", "creator", "handle", "@")):
        return "creator_cta_outro"
    if sid in {"outro", "brand-outro", "creator-cta"}:
        return "creator_cta_outro"

    candidates: list[tuple[int, str]] = []
    heading_and_id = f"{sid} {heading}"
    source_control = bool(re.search(r"\b(?:git|github|commits?|version control|pull request|diff history)\b", blob))
    mobile = any(term in blob for term in ("phone", "mobile", "app screen", "notification", "messaging"))
    explicit_3d = any(k in blob for k in ("3d", "three.js", "wireframe rig", "isometric 3d"))

    if any(k in blob for k in ("not-equal", "not equal", "≠", " vs ", "difference", "contrast", "compare", "comparison", "misconception", "alag")) and not any(k in blob for k in ("offline", "bina internet", "without internet", "no internet", "disconnected")):
        candidates.append((96, "comparison_split"))

    if explicit_3d and mobile and not source_control:
        candidates.append((98, "smartphone_ui"))
    elif explicit_3d:
        if any(k in blob for k in ("offline", "bina internet", "without internet", "no internet", "disconnected", "wifi", "wi-fi")):
            candidates.append((97, "three_node_offline"))
        else:
            candidates.append((96, "three_workstation"))

    if source_control and any(k in blob for k in ("broken", "bigad", "rollback", "undo", "purane", "wapas", "error indicator", "regression")):
        candidates.append((97, "history_snapshots"))
    elif source_control and any(k in blob for k in ("snapshot", "restore", "bug")):
        candidates.append((86, "history_snapshots"))

    if any(k in blob for k in ("offline", "bina internet", "without internet", "no internet", "disconnected", "wi-fi", "wifi")):
        candidates.append((95, "offline_workflow" if source_control else
                           "smartphone_ui" if mobile else "process_timeline"))

    if any(k in blob for k in ("search", "google", "serp", "not found")) and not any(k in heading_and_id for k in ("difference", "offline", "follow")):
        candidates.append((93, "search_serp"))

    if any(k in heading_and_id for k in ("folder", "workspace", "dropzone")) or any(k in blob for k in ("raw-footage", "style-reference", "folder drop")):
        candidates.append((93, "folder_workspace"))

    if any(k in blob for k in ("code editor", "version control", "changes track", "diff history", "explained")) and not any(k in blob for k in ("broken", "bigad", "rollback", "github", "platform")):
        candidates.append((94, "editor_history"))

    if (any(k in heading_and_id for k in ("team", "collaborat", "together", "network"))
            or (not any(k in heading_and_id for k in ("platform", "dashboard")) and any(k in blob for k in ("avatars", "team ", "build together", "developers ke", "peer", "network", "mesh")))):
        candidates.append((92, "collaboration_network"))

    if any(k in heading_and_id for k in ("platform", "dashboard", "repository", "browser")) or any(k in blob for k in ("dashboard", "file tree", "readme", "repository title")):
        if not any(k in heading_and_id for k in ("difference", "summary", "remember")):
            candidates.append((93, "platform_browser"))

    if any(k in blob for k in ("terminal", "command", "git add", "git commit", "git push", "workflow", "cli", "shell")) and not any(k in heading_and_id for k in ("history", "difference", "platform")):
        candidates.append((91, "terminal_pipeline"))

    if source_control and any(k in blob for k in ("history", "timeline")) and vtype != "three":
        candidates.append((85, "editor_history"))

    if any(k in blob for k in ("step ", "steps", "pipeline", "process")):
        candidates.append((82, "process_timeline"))

    if any(k in blob for k in ("phone", "mobile", "app screen", "notification", "instagram app")):
        candidates.append((80, "smartphone_ui"))

    if re.search(r"\b\d+(?:\.\d+)?%|\$\d|\b\d+\s*(?:kcal|protein|mg|g|x|fps|ms)\b", f"{heading} {narr}"):
        candidates.append((86, "metric_showcase"))

    if any(k in f"{blob} {niche}" for k in ("nutrition", "health", "food", "recipe", "plant", "science", "biology", "habit", "lifestyle", "routine")):
        candidates.append((81, "diagram_explainer"))

    if any(k in blob for k in ("remember", "summary", "takeaway", "yaad rakho", "recap", "difference")):
        candidates.append((90, "comparison_split"))

    if vtype == "three" and not explicit_3d and source_control:
        candidates.append((78, "editor_history"))

    candidates.extend([(70, "process_timeline"), (65, "diagram_explainer"), (60, "comparison_split")])

    # Deduplicate and avoid immediate repeat of previous scene layout when another strong candidate exists
    ordered: list[str] = []
    for _, cid in sorted(candidates, key=lambda x: x[0], reverse=True):
        if cid not in ordered:
            ordered.append(cid)
    last_used = used_recently[-1] if used_recently else None
    if ordered[0] == last_used and len(ordered) > 1:
        return ordered[1]
    return ordered[0]


def _window_box(url_html: str, body_html: str, *, badge_html: str = "", dark: bool = False,
                box_attrs: str = "", url_attrs: str = "", head_style: str = "") -> str:
    cls = "vrs-window vrs-window--dark" if dark else "vrs-window"
    attrs = f" {box_attrs}" if box_attrs else ""
    h_style = f' style="{head_style}"' if head_style else ""
    u_attrs = f" {url_attrs}" if url_attrs else ""
    return (
        f'<div class="{cls}"{attrs}>'
        f'<div class="vrs-win-head"{h_style}>'
        '<div class="vrs-win-dots"><span style="background:#ef4444"></span><span style="background:#f59e0b"></span><span style="background:#10b981"></span></div>'
        f'<div class="vrs-browser-url"{u_attrs}>{url_html}</div>'
        f'{badge_html}</div>'
        f'{body_html}</div>'
    )


def _step_cards_html(items: list[str], *, pad: str, font_size: int, pill_prefix: str) -> str:
    ev_tags = ("stage-primary", "stage-detail", "stage-finish")
    return "".join(
        f'<div class="vrs-card" data-ev="{ev_tags[idx % 3]}" style="padding:{pad};flex-direction:row;align-items:center;justify-content:space-between;gap:18px">'
        f'<div style="display:flex;align-items:center;gap:18px">'
        + (f'<div class="vrs-op-circle" style="width:58px;height:58px;font-size:26px;flex-shrink:0">{idx + 1}</div>' if pill_prefix else "")
        + f'<div style="font-size:{font_size}px;font-weight:800;line-height:1.25;color:#181412">{escape(txt)}</div></div>'
        f'<span class="vrs-pill vrs-pill--warm" style="font-size:18px;padding:6px 14px">{pill_prefix}0{idx + 1}</span></div>'
        for idx, txt in enumerate(items)
    )


def _format_heading_html(heading: str) -> str:
    clean = (heading or "").strip()
    if not clean:
        return ""
    for sym in ("≠", "⇄", "→", "="):
        if sym in clean:
            parts = clean.split(sym, 1)
            return f"{escape(parts[0].strip())} {escape(sym)} <em>{escape(parts[1].strip())}</em>"
    words = clean.split()
    if len(words) >= 2:
        split_at = max(1, len(words) - (1 if len(words) <= 2 else 2))
        return f"{escape(' '.join(words[:split_at]))} <em>{escape(' '.join(words[split_at:]))}</em>"
    return f"<em>{escape(clean)}</em>"


def _artwork_markup(storyboard):
    """Draw only the locally validated shapes authored by the production agent."""
    layers = []
    initially_hidden = {}
    for event in storyboard["events"]:
        initially_hidden.setdefault(event["target"], event["action"] in {"reveal", "type", "connect"})
        if event["action"] == "transform":
            initially_hidden.setdefault(event["value"], True)
    paints = {"ink": "var(--brand-ink, #181412)", "accent": "var(--brand-accent, #ea580c)",
              "surface": "var(--style-surface, #ffffff)", "border": "var(--style-border, #e5dbd0)"}
    for layer in storyboard["artwork"]["layers"]:
        marks = []
        for shape in layer["shapes"]:
            kind = shape["type"]
            fill = shape.get("fill", "none" if kind == "path" and "stroke" in shape else "ink")
            stroke = shape.get("stroke", "none")
            attrs = (f'fill="{paints.get(fill, fill)}" stroke="{paints.get(stroke, stroke)}" '
                     f'stroke-width="{shape.get("strokeWidth", 1)}" stroke-linejoin="round" stroke-linecap="round"')
            if kind == "path":
                marks.append(f'<path d="{escape(shape["d"])}" {attrs}/>')
                continue
            x, y, w, h = shape["box"]
            bounds = f'x="{x}" y="{y}" width="{w}" height="{h}"'
            if kind == "rect":
                marks.append(f'<rect {bounds} rx="{shape.get("radius", 0)}" {attrs}/>')
            elif kind == "ellipse":
                marks.append(f'<ellipse cx="{x+w/2}" cy="{y+h/2}" rx="{w/2}" ry="{h/2}" {attrs}/>')
            elif kind == "text":
                marks.append(f'<text x="{x}" y="{y}" dominant-baseline="hanging" font-size="{shape["size"]}" '
                             f'font-weight="700" {attrs}>{escape(shape["text"])}</text>')
            else:
                marks.append(f'<image href="assets/{escape(shape["asset"])}" {bounds}/>')
        opacity = 0 if initially_hidden.get(layer["id"]) else 1
        layers.append(f'<g data-ev="{layer["id"]}" opacity="{opacity}">{"".join(marks)}</g>')
    return ('<div class="vrs-auto-stage" style="height:auto;flex:1">'
            '<svg class="vrs-semantic-artwork" width="100%" height="100%" viewBox="0 0 944 760" '
            'aria-hidden="true" style="flex:1;min-height:0;overflow:hidden">'
            + "".join(layers) + '</svg></div>')


def _common_js_setup(sid: str, beats: list[dict], extra_js: str = "", storyboard: dict | None = None) -> str:
    sid_js = json.dumps(sid)
    beats_js = json.dumps([{"id": b["id"], "start": float(b["start"]), "end": float(b["end"])} for b in beats])
    events_js = json.dumps(storyboard.get("events", []) if storyboard else [])
    return f"""window.reelTimeline = window.reelTimeline || gsap.timeline({{paused:true}});
{{
  const scene = (window.reelScenes || []).find(s => s.id === {sid_js}) || {{start: 0, end: 5, animation: []}};
  const root = document.getElementById({sid_js});
  const tl = window.reelTimeline;
  const specBeats = {beats_js};
  const liveBeats = scene.animation && scene.animation.length ? scene.animation : specBeats;
  const evList = {events_js};
  const totalDur = Math.max(1, ...(window.reelScenes || []).map(x => Number(x.end) || 0), Number(scene.end) || 5);
  const sceneDur = Math.max(0.8, (Number(scene.end) || 5) - (Number(scene.start) || 0));
  const bAt = (idx, fallbackStart, fallbackDur) => {{
    const row = liveBeats[idx] || specBeats[idx];
    if (!row) return {{start: fallbackStart, dur: fallbackDur}};
    return {{start: Number(row.start) || 0, dur: Math.max(0.18, Number(row.end) - Number(row.start))}};
  }};
  const relPos = (node, container) => {{
    let x = node.offsetWidth * 0.62, y = node.offsetHeight * 0.56, p = node;
    while (p && p !== container) {{
      x += p.offsetLeft || 0;
      y += p.offsetTop || 0;
      p = p.offsetParent;
    }}
    return {{x, y}};
  }};
  if (root) {{
    const prog = root.querySelector(".vrs-progress-fill");
    if (prog) tl.fromTo(prog, {{scaleX:0}}, {{scaleX:1, duration:totalDur, ease:"none"}}, 0);
    const head = root.querySelector(".vrs-auto-heading");
    const stage = root.querySelector(".vrs-auto-stage, .vrs-cta-card");
    if (head) tl.fromTo(head, {{opacity:0, y:16}}, {{opacity:1, y:0, duration:0.34, ease:"power3.out", immediateRender:false}}, scene.start);
    if (stage) tl.fromTo(stage, {{opacity:0, y:18}}, {{opacity:1, y:0, duration:0.36, ease:"expo.out", immediateRender:false}}, scene.start + 0.03);
    root.querySelectorAll(".js-beat").forEach((el, idx) => {{
      if (el.hasAttribute("data-ev")) return;
      const beat = bAt(Math.min(idx + 1, liveBeats.length - 1), 0.14 + idx * 0.24, 0.36);
      const startAt = scene.start + Math.min(beat.start, 0.20 + idx * 0.28);
      tl.fromTo(el, {{opacity:0, y:18, scale:0.97}}, {{opacity:1, y:0, scale:1, duration:0.34, ease:"expo.out", immediateRender:false}}, startAt);
    }});
    root.querySelectorAll(".js-stamp").forEach((el, idx) => {{
      if (el.hasAttribute("data-ev")) return;
      const beat = bAt(Math.min(idx + 2, liveBeats.length - 1), 0.72 + idx * 0.28, 0.36);
      const startAt = scene.start + Math.min(beat.start, 0.85 + idx * 0.32);
      tl.fromTo(el, {{opacity:0, scale:1.45, rotation:-14}}, {{opacity:1, scale:1, rotation:-7, duration:0.34, ease:"back.out(1.7)", immediateRender:false}}, startAt);
    }});
    root.querySelectorAll(".js-draw").forEach((el, idx) => {{
      if (el.closest("[data-ev]")) return;
      const totalLen = typeof el.getTotalLength === "function" ? el.getTotalLength() : 360;
      const startAt = scene.start + Math.min(0.32 + idx * 0.24, Math.max(0.18, sceneDur * 0.35));
      tl.fromTo(el, {{strokeDasharray: totalLen, strokeDashoffset: totalLen}}, {{strokeDashoffset: 0, duration:0.85, ease:"power2.inOut", immediateRender:false}}, startAt);
    }});
    root.querySelectorAll(".js-bar").forEach((el, idx) => {{
      if (el.closest("[data-ev]")) return;
      const targetW = el.dataset.width || "100%";
      const startAt = scene.start + 0.35 + idx * 0.30;
      tl.fromTo(el, {{width:"0%"}}, {{width:targetW, duration:0.80, ease:"power2.inOut", immediateRender:false}}, startAt);
    }});
    const cur = root.querySelector(".vrs-cursor-ptr");
    if (cur) tl.set(cur, {{opacity:0}}, scene.start);
    const artwork = root.querySelector(".vrs-semantic-artwork");
    evList.forEach((ev) => {{
      const el = root.querySelector('[data-ev="' + ev.target + '"]');
      if (!el) return;
      if (artwork) {{
        const t = scene.start + ev.at;
        const duration = ev.duration;
        if (ev.action === "transform") {{
          const destination = artwork.querySelector('[data-ev="' + ev.value + '"]');
          tl.to(el, {{opacity:0, duration}}, t);
          tl.to(destination, {{opacity:1, duration}}, t);
        }} else if (ev.action === "connect") {{
          tl.set(el, {{opacity:1}}, t);
          el.querySelectorAll('path:not([stroke="none"])').forEach(path => {{
            const length = path.getTotalLength();
            tl.fromTo(path, {{strokeDasharray:length, strokeDashoffset:length}},
              {{strokeDashoffset:0, duration, ease:"power2.inOut", immediateRender:false}}, t);
          }});
        }} else if (ev.action === "type") {{
          tl.set(el, {{opacity:1}}, t);
          tl.fromTo(el, {{clipPath:"inset(0 100% 0 0)"}},
            {{clipPath:"inset(0 0 0 0)", duration, ease:"steps(16)", immediateRender:false}}, t);
        }} else if (ev.action === "highlight") {{
          tl.to(el, {{scale:1.08, transformOrigin:"50% 50%", duration:duration/2, yoyo:true, repeat:1}}, t);
        }} else if (ev.action === "focus") {{
          const box = el.getBBox();
          const x = gsap.utils.clamp(0, 104, box.x + box.width/2 - 420);
          const y = gsap.utils.clamp(0, 84, box.y + box.height/2 - 338);
          tl.fromTo(artwork, {{attr:{{viewBox:"0 0 944 760"}}}},
            {{attr:{{viewBox:x+" "+y+" 840 676"}}, duration:duration/2, yoyo:true, repeat:1,
              ease:"power2.inOut", immediateRender:false}}, t);
        }} else {{
          tl.to(el, {{opacity:ev.action === "exit" ? 0 : 1, duration, ease:"power2.out"}}, t);
        }}
        return;
      }}
      const relAt = Math.max(0.04, Math.min(sceneDur - 0.16, Number(ev.at) || 0.18));
      const tEv = scene.start + relAt;
      const dEv = Math.max(0.18, Number(ev.duration) || 0.38);
      if (ev.action === "reveal" || ev.action === "compare") {{
        if (relAt > 0.35) tl.set(el, {{opacity:0, y:20, scale:0.97}}, scene.start);
        tl.fromTo(el, {{opacity:0, y:20, scale:0.97}}, {{opacity:1, y:0, scale:1, duration:dEv, ease:"expo.out", immediateRender:false}}, tEv);
        el.querySelectorAll(".js-bar").forEach((b, bi) => tl.fromTo(b, {{width:"0%"}}, {{width:b.dataset.width || "100%", duration:0.65, ease:"power2.out", immediateRender:false}}, tEv + 0.12 + bi * 0.12));
      }} else if (ev.action === "type") {{
        tl.set(el, {{opacity:0}}, scene.start);
        tl.to(el, {{opacity:1, duration:0.12}}, tEv);
        const clip = el.querySelector(".vrs-type-clip") || el;
        tl.fromTo(clip, {{clipPath:"inset(0 100% 0 0)", borderRightColor:"var(--brand-accent, #ea580c)"}}, {{clipPath:"inset(0 0% 0 0)", duration:dEv, ease:"steps(22)", immediateRender:true}}, tEv);
        tl.to(clip, {{borderRightColor:"transparent", duration:0.05}}, tEv + dEv + 0.04);
        root.querySelectorAll('[data-ev-after="' + ev.target + '"]').forEach((sub, si) => {{
          tl.set(sub, {{opacity:0, y:8}}, scene.start);
          tl.to(sub, {{opacity:1, y:0, duration:0.26, ease:"power2.out"}}, tEv + dEv + 0.06 + si * 0.08);
        }});
      }} else if (ev.action === "click") {{
        if (cur && cur.offsetParent) {{
          const pos = relPos(el, cur.offsetParent);
          const pre = Math.max(scene.start + 0.05, tEv - 0.42);
          tl.fromTo(cur, {{opacity:0, x:Math.max(40, pos.x - 150), y:pos.y + 120, scale:1}}, {{opacity:1, x:pos.x, y:pos.y, duration:0.38, ease:"power2.out", immediateRender:false}}, pre);
          tl.to(cur, {{scale:0.80, duration:0.09, yoyo:true, repeat:1}}, tEv);
          tl.to(cur, {{opacity:0, duration:0.22, immediateRender:false}}, tEv + 0.45);
        }}
        const ring = el.querySelector(".vrs-click-ring");
        if (ring) {{
          tl.set(ring, {{opacity:0, scale:0.4}}, scene.start);
          tl.fromTo(ring, {{opacity:0.95, scale:0.4}}, {{opacity:0, scale:2.3, duration:0.42, ease:"power2.out", immediateRender:false}}, tEv);
        }}
        tl.to(el, {{scale:0.96, duration:0.09, yoyo:true, repeat:1}}, tEv);
        const activeBg = el.dataset.activeBg || "#059669";
        const activeColor = el.dataset.activeColor || "#ffffff";
        const activeText = el.dataset.activeText;
        tl.to(el, {{backgroundColor:activeBg, color:activeColor, borderColor:activeBg, duration:0.20}}, tEv + 0.10);
        if (activeText) {{
          const lblTarget = el.querySelector("[data-click-label]") || el;
          const origText = lblTarget.textContent;
          const swObj = {{v:0}};
          tl.to(lblTarget, {{color:activeColor, duration:0.18}}, tEv + 0.08);
          tl.to(swObj, {{v:1, duration:0.04, onUpdate:() => {{ lblTarget.textContent = swObj.v >= 0.5 ? activeText : origText; }}}}, tEv + 0.08);
        }}
      }} else if (ev.action === "connect") {{
        el.querySelectorAll("path, line").forEach((p, pi) => {{
          const len = typeof p.getTotalLength === "function" ? p.getTotalLength() : 320;
          tl.set(p, {{strokeDasharray:len, strokeDashoffset:len}}, scene.start);
          tl.to(p, {{strokeDashoffset:0, duration:dEv, ease:"power2.inOut"}}, tEv + pi * 0.08);
        }});
        el.querySelectorAll(".js-fly-pkt").forEach((pkt, pi) => {{
          const x0 = parseFloat(pkt.dataset.pktX0 || "0");
          const x1 = parseFloat(pkt.dataset.pktX1 || "360");
          const y0 = parseFloat(pkt.dataset.pktY0 || "0");
          const arcY = parseFloat(pkt.dataset.pktArc || "-32");
          const pst = tEv + pi * 0.16;
          tl.set(pkt, {{opacity:0, x:x0, y:y0, scale:0.85}}, scene.start);
          tl.to(pkt, {{opacity:1, scale:1, duration:0.14}}, pst);
          tl.to(pkt, {{x:(x0 + x1) * 0.5, y:y0 + arcY, duration:dEv * 0.45, ease:"power1.out"}}, pst + 0.06);
          tl.to(pkt, {{x:x1, y:y0, duration:dEv * 0.45, ease:"power1.in"}}, pst + 0.06 + dEv * 0.45);
          tl.to(pkt, {{opacity:0, scale:0.75, duration:0.16}}, pst + dEv * 0.96);
        }});
        if (relAt > 0.30) tl.set(el, {{opacity:0, y:16}}, scene.start);
        tl.to(el, {{opacity:1, y:0, duration:0.28, ease:"expo.out"}}, tEv);
        root.querySelectorAll('[data-ev-after="' + ev.target + '"]').forEach((sub) => {{
          tl.set(sub, {{opacity:0, y:6}}, scene.start);
          tl.to(sub, {{opacity:1, y:0, duration:0.28, ease:"power2.out"}}, tEv + dEv * 0.65);
        }});
      }} else if (ev.action === "count") {{
        el.querySelectorAll("[data-count-to]").forEach((cnt) => {{
          const to = parseFloat(cnt.dataset.countTo || "0");
          const pre = cnt.dataset.countPrefix || "";
          const suf = cnt.dataset.countSuffix || "";
          const dec = parseInt(cnt.dataset.countDec || "0", 10);
          const obj = {{v:0}};
          tl.to(obj, {{v:to, duration:dEv, ease:"power2.out", onUpdate:() => {{ cnt.textContent = pre + obj.v.toFixed(dec) + suf; }}}}, tEv);
        }});
        el.querySelectorAll("circle.js-draw, path.js-draw").forEach((p) => {{
          const len = typeof p.getTotalLength === "function" ? p.getTotalLength() : 360;
          const pct = Math.max(0.1, Math.min(1, parseFloat(p.dataset.pct || "0.92")));
          tl.set(p, {{strokeDasharray:len, strokeDashoffset:len}}, scene.start);
          tl.to(p, {{strokeDashoffset:len * (1 - pct), duration:dEv, ease:"power2.out"}}, tEv);
        }});
        el.querySelectorAll(".js-bar").forEach((b) => tl.fromTo(b, {{width:"0%"}}, {{width:b.dataset.width || "90%", duration:dEv, ease:"power2.out", immediateRender:false}}, tEv));
      }} else if (ev.action === "confirm" || ev.action === "reject" || ev.action === "restore") {{
        if (ev.action === "restore") {{
          el.querySelectorAll("path, line").forEach((p) => {{
            const len = typeof p.getTotalLength === "function" ? p.getTotalLength() : 360;
            tl.set(p, {{strokeDasharray:len, strokeDashoffset:len}}, scene.start);
            tl.to(p, {{strokeDashoffset:0, duration:dEv, ease:"power2.inOut"}}, tEv);
          }});
          root.querySelectorAll("[data-broken-layer]").forEach((b) => tl.to(b, {{opacity:0, duration:0.16}}, tEv));
          root.querySelectorAll("[data-ok-layer]").forEach((ok) => tl.to(ok, {{opacity:1, duration:0.16}}, tEv));
          root.querySelectorAll("[data-restored-layer]").forEach((r) => {{
            tl.set(r, {{opacity:0}}, scene.start);
            tl.to(r, {{opacity:1, duration:0.26, ease:"power2.out"}}, tEv + 0.08);
          }});
          root.querySelectorAll("[data-restored-dim]").forEach((d) => tl.to(d, {{opacity:0.28, duration:0.22}}, tEv + 0.08));
          const banner = root.querySelector('[data-ev="restore-banner"]');
          if (banner) {{ tl.set(banner, {{opacity:0, y:10}}, scene.start); tl.to(banner, {{opacity:1, y:0, duration:0.30, ease:"power2.out"}}, tEv + 0.14); }}
        }} else if (ev.action === "reject") {{
          root.querySelectorAll("[data-ok-layer]").forEach((ok) => tl.to(ok, {{opacity:0, duration:0.12}}, tEv));
          root.querySelectorAll("[data-broken-layer]").forEach((b) => {{
            tl.set(b, {{opacity:0}}, scene.start);
            tl.to(b, {{opacity:1, duration:0.16}}, tEv);
          }});
          root.querySelectorAll("[data-shake-target]").forEach((sh) => {{
            tl.to(sh, {{x:-6, duration:0.04}}, tEv)
              .to(sh, {{x:6, duration:0.05}}, tEv + 0.04)
              .to(sh, {{x:-5, duration:0.05}}, tEv + 0.09)
              .to(sh, {{x:4, duration:0.05}}, tEv + 0.14)
              .to(sh, {{x:0, duration:0.06}}, tEv + 0.19);
          }});
          root.querySelectorAll("[data-broken-stamp]").forEach((st) => {{
            tl.set(st, {{opacity:0, scale:1.35, rotation:-14}}, scene.start);
            tl.to(st, {{opacity:1, scale:1, rotation:-7, duration:0.32, ease:"back.out(1.7)"}}, tEv + 0.06);
          }});
          if (el.hasAttribute("data-broken-layer") || el.classList.contains("vrs-rubber-stamp")) {{
            const rot = el.classList.contains("vrs-rubber-stamp") ? -7 : 0;
            tl.set(el, {{opacity:0}}, scene.start);
            tl.to(el, {{opacity:1, rotation:rot, duration:dEv}}, tEv);
          }}
        }} else {{
          const stamp = el.classList.contains("vrs-rubber-stamp") ? el : el.querySelector(".vrs-rubber-stamp");
          if (stamp === el) {{
            tl.set(el, {{opacity:0, scale:1.35, rotation:-13}}, scene.start);
            tl.to(el, {{opacity:1, scale:1, rotation:-7, duration:dEv, ease:"back.out(1.6)"}}, tEv);
          }} else {{
            if (!el.hasAttribute("data-keep-visible")) {{
              tl.set(el, {{opacity:0, y:16, scale:0.97}}, scene.start);
              tl.to(el, {{opacity:1, y:0, scale:1, duration:dEv, ease:"expo.out"}}, tEv);
            }}
            if (stamp) {{
              tl.set(stamp, {{opacity:0, scale:1.25, rotation:-13}}, scene.start);
              tl.to(stamp, {{opacity:1, scale:1, rotation:-7, duration:dEv, ease:"back.out(1.6)"}}, tEv + 0.06);
            }}
          }}
          root.querySelectorAll('[data-arrival-show="' + ev.target + '"]').forEach((a) => {{
            tl.set(a, {{opacity:0}}, scene.start);
            tl.to(a, {{opacity:1, duration:0.25}}, tEv);
          }});
          root.querySelectorAll('[data-arrival-hide="' + ev.target + '"]').forEach((h) => tl.to(h, {{opacity:0, duration:0.20}}, tEv));
          if (el.dataset.recoilLeft) {{
            const l = root.querySelector(el.dataset.recoilLeft);
            const r = root.querySelector(el.dataset.recoilRight || "");
            if (l) tl.to(l, {{x:-10, duration:0.14, yoyo:true, repeat:1, ease:"power2.out"}}, tEv);
            if (r) tl.to(r, {{x:10, duration:0.14, yoyo:true, repeat:1, ease:"power2.out"}}, tEv);
          }}
          const swapEl = el.querySelector("[data-confirm-swap]") || (el.hasAttribute("data-confirm-swap") ? el : null);
          if (swapEl) {{
            const origS = swapEl.textContent;
            const newS = swapEl.dataset.confirmSwap;
            const sObj = {{v:0}};
            tl.to(sObj, {{v:1, duration:0.04, onUpdate:() => {{ swapEl.textContent = sObj.v >= 0.5 ? newS : origS; }}}}, tEv);
          }}
        }}
      }} else if (ev.action === "transform" || ev.action === "highlight") {{
        const knob = el.querySelector(".vrs-toggle-knob");
        const sw = el.querySelector(".vrs-toggle-switch");
        const lbl = el.querySelector("[data-off-text]");
        if (knob && sw) {{
          tl.to(knob, {{x:0, duration:0.24, ease:"power2.inOut"}}, tEv);
          tl.to(sw, {{backgroundColor:"#dc2626", duration:0.22}}, tEv);
          if (lbl) {{
            const onLbl = lbl.textContent;
            const offLbl = lbl.dataset.offText || "OFF";
            const lblObj = {{v:0}};
            tl.to(lblObj, {{v:1, duration:0.04, onUpdate:() => {{ lbl.textContent = lblObj.v >= 0.5 ? offLbl : onLbl; }}}}, tEv + 0.08);
            tl.to(lbl, {{color:"#991b1b", duration:0.18}}, tEv + 0.08);
          }}
        }} else {{
          tl.fromTo(el, {{opacity:0.45, scale:0.96}}, {{opacity:1, scale:1.02, duration:dEv, ease:"power2.out", immediateRender:false}}, tEv);
          tl.to(el, {{scale:1, duration:0.22}}, tEv + dEv);
        }}
      }} else if (ev.action === "drag") {{
        tl.to(el, {{x:16, y:-10, duration:dEv, ease:"power2.inOut", yoyo:true, repeat:1}}, tEv);
      }} else if (ev.action === "exit") {{
        tl.to(el, {{opacity:0.35, duration:dEv}}, tEv);
      }}
    }});
  }}
  {extra_js}
}}
"""


def _three_js_script(
    sid: str,
    mode: str,
    accent_hex: str,
    secondary_hex: str,
    heading: str = "",
    labels: list[str] | None = None,
    commands: list[str] | None = None,
) -> str:
    sid_js = json.dumps(sid)
    mode_js = json.dumps(mode)
    ctx_js = json.dumps({
        "heading": heading or sid,
        "labels": (labels or [])[:4],
        "commands": (commands or [])[:3],
    })
    accent_num = accent_hex.lstrip("#")
    secondary_num = secondary_hex.lstrip("#")
    return f"""  window.__hf = window.__hf || {{}};
  window.__hf.buildReady = window.__hf.buildReady || {{}};
  window.__hf.buildReady[{sid_js}] = (async () => {{
    const mount = document.getElementById("three-" + {sid_js});
    if (!mount) return;
    const T = await import(new URL("assets/three.module.min.js", document.baseURI).href);
    const ctx = {ctx_js};
    const mode = {mode_js};
    const w = 944, h = 708;
    const renderer = new T.WebGLRenderer({{alpha:true, antialias:true, powerPreference:"low-power"}});
    renderer.setSize(w, h, false);
    renderer.setClearColor(0x000000, 0);
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = T.PCFSoftShadowMap;
    renderer.toneMapping = T.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.18;
    mount.append(renderer.domElement);

    const world = new T.Scene();
    const camera = new T.PerspectiveCamera(32, w / h, 0.1, 80);

    // Studio 3-point lighting + emissive core aura
    world.add(new T.HemisphereLight(0xdcebff, 0x162044, 1.45));
    const key = new T.DirectionalLight(0xfffaf3, 2.9);
    key.position.set(-5.2, 10.8, 6.2);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    key.shadow.camera.near = 2;
    key.shadow.camera.far = 28;
    key.shadow.camera.left = -6.5;
    key.shadow.camera.right = 6.5;
    key.shadow.camera.top = 6.5;
    key.shadow.camera.bottom = -6.5;
    key.shadow.bias = -0.0012;
    world.add(key);

    const rim = new T.DirectionalLight(0x38bdf8, 1.9);
    rim.position.set(6.2, 5.2, -5.8);
    world.add(rim);

    const aura = new T.PointLight(mode === "offline" ? 0x10b981 : 0x38bdf8, 3.8, 9.5, 1.5);
    aura.position.set(0.2, 2.2, 1.0);
    world.add(aura);

    const mat = (color, opts = {{}}) => new T.MeshStandardMaterial({{color, roughness: 0.38, metalness: 0.22, ...opts}});
    const deckDark = mat(0x111a3d, {{roughness: 0.56, metalness: 0.28}});
    const deckTrim = mat(0x7c97e0, {{roughness: 0.28, metalness: 0.45}});
    const deckTop = mat(0x23356d, {{roughness: 0.46, metalness: 0.20}});
    const aluminum = mat(0xd8e0ee, {{roughness: 0.24, metalness: 0.72}});
    const darkMetal = mat(0x1e293b, {{roughness: 0.34, metalness: 0.58}});
    const accentMat = mat(0x{accent_num}, {{emissive: 0x{accent_num}, emissiveIntensity: 0.45, roughness: 0.26}});
    const secMat = mat(0x{secondary_num}, {{emissive: 0x{secondary_num}, emissiveIntensity: 0.50, roughness: 0.25}});
    const glassMat = new T.MeshPhysicalMaterial({{color: 0x93c5fd, transparent: true, opacity: 0.42, roughness: 0.12, metalness: 0.1, transmission: 0.45}});
    const emeraldMat = mat(0x10b981, {{emissive: 0x059669, emissiveIntensity: 1.05, roughness: 0.22}});
    const rubyMat = mat(0xef4444, {{emissive: 0xdc2626, emissiveIntensity: 0.95, roughness: 0.25}});

    const add = (g, m, x, y, z, parent = world, cast = true, recv = true) => {{
      const o = new T.Mesh(g, m);
      o.position.set(x, y, z);
      o.castShadow = cast;
      o.receiveShadow = recv;
      parent.add(o);
      return o;
    }};

    function makeScreenTexture(kind) {{
      const c = document.createElement("canvas");
      const cw = kind === "phone" ? 512 : 1024;
      const ch = kind === "phone" ? 960 : 640;
      c.width = cw;
      c.height = ch;
      const g = c.getContext("2d");
      g.fillStyle = "#0b1120";
      g.fillRect(0, 0, cw, ch);
      if (kind === "phone") {{
        const grad = g.createLinearGradient(0, 0, cw, ch);
        grad.addColorStop(0, "#111827");
        grad.addColorStop(1, "#090f1d");
        g.fillStyle = grad;
        g.fillRect(0, 0, cw, ch);
        g.fillStyle = "#94a3b8";
        g.font = "bold 24px sans-serif";
        g.fillText("9:41", 38, 52);
        g.fillStyle = "#10b981";
        g.fillRect(cw - 110, 32, 68, 22);
        g.fillStyle = "#1e293b";
        g.fillRect(28, 92, cw - 56, 150);
        g.fillStyle = "#f8fafc";
        g.font = "bold 32px sans-serif";
        g.fillText(String(ctx.heading).slice(0, 22), 52, 155);
        g.fillStyle = "#38bdf8";
        g.font = "bold 24px monospace";
        g.fillText("LIVE · SYNCED ✓", 52, 206);
        // Analytics Sparkline Card
        g.fillStyle = "#162238";
        g.fillRect(28, 268, cw - 56, 260);
        g.strokeStyle = "#{accent_num}";
        g.lineWidth = 8;
        g.beginPath();
        [470, 430, 445, 380, 345, 315].forEach((py, i) => {{
          const px = 62 + i * 74;
          if (i === 0) g.moveTo(px, py); else g.lineTo(px, py);
        }});
        g.stroke();
        // Bottom action cards
        g.fillStyle = "#059669";
        g.fillRect(28, 560, cw - 56, 120);
        g.fillStyle = "#ffffff";
        g.font = "bold 28px sans-serif";
        g.fillText(String((ctx.labels && ctx.labels[0]) || "READY TO SHIP").slice(0, 24), 56, 630);
        g.fillStyle = "#1e293b";
        g.fillRect(28, 706, cw - 56, 120);
        g.fillStyle = "#cbd5e1";
        g.font = "bold 24px monospace";
        g.fillText(String((ctx.commands && ctx.commands[0]) || "v2.4 Verified").slice(0, 26), 56, 774);
      }} else {{
        // Top Window Bar
        g.fillStyle = "#1e293b";
        g.fillRect(0, 0, cw, 54);
        ["#ef4444", "#f59e0b", "#10b981"].forEach((col, i) => {{
          g.fillStyle = col;
          g.beginPath();
          g.arc(34 + i * 28, 27, 9, 0, Math.PI * 2);
          g.fill();
        }});
        g.fillStyle = "#cbd5e1";
        g.font = "bold 22px monospace";
        g.fillText(mode === "offline" ? "local-repo — AIR-GAPPED OK" : "studio-workspace — main", 145, 34);
        // Sidebar file tree
        g.fillStyle = "#0f172a";
        g.fillRect(0, 54, 240, ch - 54);
        g.fillStyle = "#38bdf8";
        g.font = "bold 20px monospace";
        ["▸ src/", "  app.js", "  engine.js", "▸ .git/", "  HEAD", "  objects/"].forEach((row, i) => {{
          g.fillStyle = row.includes("git") ? "#fbbf24" : "#94a3b8";
          g.fillText(row, 26, 108 + i * 42);
        }});
        // Code Editor Pane
        g.fillStyle = "#34d399";
        g.fillRect(265, 92, 6, 48);
        g.fillStyle = "#e2e8f0";
        g.font = "22px monospace";
        g.fillText("+ const commit = snapshot(state);", 288, 122);
        g.fillStyle = "#38bdf8";
        g.fillText("  repo.saveLocal({{ offline: true }});", 288, 174);
        g.fillStyle = "#a78bfa";
        g.fillText("  timeline.verifyChecksum(hash);", 288, 226);
        // Integrated Terminal bottom half
        g.fillStyle = "#090d1a";
        g.fillRect(240, 320, cw - 240, ch - 320);
        g.fillStyle = "#334155";
        g.fillRect(240, 320, cw - 240, 3);
        const lines = (ctx.commands && ctx.commands.length) ? ctx.commands : ["git add app.js", "git commit -m 'v2'", "git log --oneline"];
        lines.slice(0, 3).forEach((ln, idx) => {{
          g.fillStyle = "#38bdf8";
          g.font = "bold 24px monospace";
          g.fillText("$", 272, 388 + idx * 62);
          g.fillStyle = "#f8fafc";
          g.fillText(String(ln).slice(0, 34), 302, 388 + idx * 62);
        }});
        g.fillStyle = mode === "offline" ? "#10b981" : "#{accent_num}";
        g.fillRect(272, 566, 260, 40);
        g.fillStyle = "#ffffff";
        g.font = "bold 20px sans-serif";
        g.fillText(mode === "offline" ? "100% LOCAL · NO WIFI" : "COMMIT TRACKED ✓", 292, 593);
      }}
      const tex = new T.CanvasTexture(c);
      tex.colorSpace = T.SRGBColorSpace;
      return tex;
    }}

    // Sculpted Studio Plinth
    add(new T.BoxGeometry(10.2, 0.52, 6.8), deckDark, 0, -0.82, 0, world, false, true);
    add(new T.BoxGeometry(9.96, 0.14, 6.56), deckTrim, 0, -0.50, 0, world, true, true);
    add(new T.BoxGeometry(9.64, 0.18, 6.24), deckTop, 0, -0.35, 0, world, false, true);
    for (let i = -3; i <= 3; i += 2) {{
      add(new T.BoxGeometry(8.8, 0.025, 0.045), mat(0x4a66b8, {{roughness: 0.5}}), 0, -0.25, i * 0.8, world, false, true);
    }}

    const pulses = [];
    const buildCurvePipe = (pts, material) => {{
      const curve = new T.CatmullRomCurve3(pts.map(p => new T.Vector3(...p)));
      add(new T.TubeGeometry(curve, 36, 0.045, 10, false), material, 0, 0, 0, world, true, false);
      const bead = add(new T.SphereGeometry(0.11, 16, 16), emeraldMat, pts[0][0], pts[0][1], pts[0][2], world, false, false);
      pulses.push({{curve, bead, phase: pulses.length * 0.34}});
      return curve;
    }};

    let heroObj = null;
    const satellites = [];

    if (mode === "smartphone") {{
      // Procedural Titanium Smartphone Rig centered heroically
      const phone = new T.Group();
      add(new T.BoxGeometry(2.38, 4.72, 0.24), aluminum, 0, 0, 0, phone, true, true);
      add(new T.BoxGeometry(2.26, 4.60, 0.26), darkMetal, 0, 0, 0, phone, true, false);
      const scrTex = makeScreenTexture("phone");
      add(
        new T.PlaneGeometry(2.12, 4.40),
        new T.MeshStandardMaterial({{map: scrTex, emissiveMap: scrTex, emissive: 0xffffff, emissiveIntensity: 0.65, roughness: 0.15}}),
        0, 0, 0.135, phone, false, false
      );
      // Dynamic island & side buttons
      add(new T.BoxGeometry(0.56, 0.11, 0.04), darkMetal, 0, 2.02, 0.145, phone, false, false);
      add(new T.BoxGeometry(0.06, 0.65, 0.08), aluminum, 1.21, 0.65, 0, phone);
      phone.scale.setScalar(0.76);
      phone.position.set(0, 1.42, 0.15);
      phone.rotation.set(-0.08, -0.22, 0.03);
      world.add(phone);
      heroObj = phone;

      // Glowing pedestal & orbiting telemetry ring
      add(new T.CylinderGeometry(1.55, 1.75, 0.22, 40), darkMetal, 0, -0.35, 0.15);
      const holoRing = add(new T.TorusGeometry(1.85, 0.04, 16, 64), secMat, 0, 1.42, 0.15, world, false, false);
      holoRing.rotation.x = 1.25;
      satellites.push(holoRing);
    }} else {{
      // Detailed Aluminum MacBook Pro Assembly
      const laptop = new T.Group();
      add(new T.BoxGeometry(3.85, 0.16, 2.62), aluminum, 0, 0, 0, laptop, true, true);
      // Touchpad & recessed keyboard well
      add(new T.BoxGeometry(1.38, 0.02, 0.82), mat(0x94a3b8, {{roughness: 0.32, metalness: 0.6}}), 0, 0.085, 0.78, laptop, false, true);
      add(new T.BoxGeometry(3.38, 0.03, 1.22), darkMetal, 0, 0.075, -0.28, laptop, false, true);
      for (let r = 0; r < 4; r++) {{
        for (let c = 0; c < 11; c++) {{
          const kw = (r === 3 && c === 5) ? 0.92 : 0.24;
          if (r === 3 && (c === 4 || c === 6)) continue;
          add(new T.BoxGeometry(kw, 0.045, 0.21), mat(0x0f172a, {{roughness: 0.4}}), -1.45 + c * 0.29, 0.105, -0.70 + r * 0.27, laptop, true, false);
        }}
      }}
      // Hinge barrel + angled Retina display lid
      const hinge = add(new T.CylinderGeometry(0.085, 0.085, 3.35, 18), darkMetal, 0, 0.10, -1.27, laptop);
      hinge.rotation.z = Math.PI / 2;
      const pivot = new T.Group();
      pivot.position.set(0, 0.10, -1.27);
      add(new T.BoxGeometry(3.85, 2.48, 0.09), aluminum, 0, 1.24, 0, pivot, true, true);
      add(new T.BoxGeometry(3.70, 2.34, 0.10), darkMetal, 0, 1.24, 0.01, pivot, false, false);
      const scrTex = makeScreenTexture("laptop");
      add(
        new T.PlaneGeometry(3.52, 2.16),
        new T.MeshStandardMaterial({{map: scrTex, emissiveMap: scrTex, emissive: 0xffffff, emissiveIntensity: 0.72, roughness: 0.18}}),
        0, 1.24, 0.065, pivot, false, false
      );
      pivot.rotation.x = -0.22;
      laptop.add(pivot);
      laptop.scale.setScalar(0.90);
      laptop.position.set(-1.15, -0.16, 0.22);
      laptop.rotation.y = 0.30;
      world.add(laptop);
      heroObj = laptop;

      const makeSlabTexture = (ver, label, colHex) => {{
        const c = document.createElement("canvas");
        c.width = 256; c.height = 320;
        const g = c.getContext("2d");
        g.fillStyle = "#111827";
        g.fillRect(0, 0, 256, 320);
        g.strokeStyle = colHex;
        g.lineWidth = 8;
        g.strokeRect(10, 10, 236, 300);
        g.fillStyle = colHex;
        g.fillRect(26, 28, 120, 38);
        g.fillStyle = "#0b1120";
        g.font = "bold 24px monospace";
        g.fillText(ver, 40, 55);
        g.fillStyle = "#f8fafc";
        g.font = "bold 26px sans-serif";
        g.fillText(label, 28, 120);
        g.fillStyle = "#334155";
        g.fillRect(28, 152, 200, 14);
        g.fillRect(28, 184, 165, 14);
        g.fillStyle = colHex;
        g.fillRect(28, 238, 180, 12);
        const t = new T.CanvasTexture(c);
        t.colorSpace = T.SRGBColorSpace;
        return t;
      }};

      const cardSpecs = [
        ["v1.0", "INIT", "#38bdf8"],
        ["v2.0", "DIFF +", "#818cf8"],
        ["v3.0", mode === "offline" ? "LOCAL ✓" : "SAVED ✓", "#10b981"],
      ];
      cardSpecs.forEach(([ver, title, col], i) => {{
        const cardGroup = new T.Group();
        add(new T.BoxGeometry(1.36, 1.62, 0.08), darkMetal, 0, 0, 0, cardGroup, true, true);
        const tex = makeSlabTexture(ver, title, col);
        add(
          new T.PlaneGeometry(1.26, 1.52),
          new T.MeshStandardMaterial({{map: tex, emissiveMap: tex, emissive: 0xffffff, emissiveIntensity: 0.45, roughness: 0.25}}),
          0, 0, 0.045, cardGroup, false, false
        );
        cardGroup.position.set(1.85 + i * 0.34, 0.68 + i * 0.30, -0.52 + i * 0.56);
        cardGroup.rotation.y = -0.32;
        world.add(cardGroup);
        satellites.push(cardGroup);
      }});
      buildCurvePipe([[0.05, 0.12, -0.15], [0.95, 0.55, -0.25], [1.85, 0.92, -0.15]], accentMat);
      buildCurvePipe([[0.30, 0.10, 0.35], [1.35, 0.85, 0.32], [2.48, 1.28, 0.52]], secMat);

      if (mode === "offline") {{
        // Protective Local Security Vault Ring + Dimmed Disconnected Cloud Relay
        const shieldRing = add(new T.TorusGeometry(1.22, 0.065, 16, 48), emeraldMat, 2.25, 1.12, 0.15, world, false, false);
        shieldRing.rotation.x = 1.15;
        satellites.push(shieldRing);
        const deadCloud = add(new T.OctahedronGeometry(0.42, 0), rubyMat, 3.45, 2.15, -1.15);
        satellites.push(deadCloud);
      }} else {{
        const coreOcta = add(new T.OctahedronGeometry(0.48, 0), secMat, 2.45, 2.25, 0.22);
        satellites.push(coreOcta);
      }}
    }}

    function draw(seconds) {{
      const dur = Math.max(1, scene.end - scene.start);
      const p = Math.max(0, Math.min(1, (seconds - scene.start) / dur));
      // Smooth cinema arc + gentle dolly
      if (mode === "smartphone") {{
        camera.position.set(
          Math.sin(p * Math.PI * 0.85) * 0.95,
          2.25 + Math.cos(p * Math.PI) * 0.18,
          9.65 - p * 0.55
        );
        camera.lookAt(0, 1.38, 0);
      }} else {{
        camera.position.set(
          -0.25 + Math.sin(p * Math.PI * 0.85) * 1.05,
          2.85 + Math.cos(p * Math.PI) * 0.22,
          9.15 - p * 0.62
        );
        camera.lookAt(0.22, 0.78, 0);
      }}
      if (mode === "smartphone" && heroObj) {{
        heroObj.rotation.y = -0.35 + Math.sin(p * Math.PI) * 0.62;
        heroObj.position.y = 1.42 + Math.sin(p * Math.PI * 2) * 0.06;
      }}
      satellites.forEach((s, idx) => {{
        if (s.geometry?.type === "OctahedronGeometry") {{
          s.rotation.y = p * 4.2;
          s.position.y += Math.sin(p * 6 + idx) * 0.002;
        }} else if (s.geometry?.type === "TorusGeometry") {{
          s.rotation.z = p * 3.4;
        }} else {{
          const reveal = Math.max(0.1, Math.min(1, (p - idx * 0.12) * 3.0));
          s.scale.setScalar(0.6 + 0.4 * reveal);
        }}
      }});
      pulses.forEach(item => {{
        const pt = item.curve.getPointAt((p * 1.4 + item.phase) % 1);
        item.bead.position.copy(pt);
      }});
      renderer.render(world, camera);
    }}
    draw(scene.start);
    window.addEventListener("hf-seek", event => {{
      const seconds = Number(event.detail?.time ?? event.detail ?? 0);
      if (seconds >= scene.start - 0.05 && seconds <= scene.end + 0.08) draw(seconds);
    }});
  }})();"""


def _render_component_html_and_js(
    component: str,
    scene: dict,
    brief: dict,
    brand: dict,
    style: dict,
    has_creator_photo: bool,
    *,
    scene_words: list[dict] | None = None,
    actual_start: float | None = None,
    actual_dur: float | None = None,
    assignment: dict | None = None,
) -> tuple[str, str, dict]:
    sid = scene["id"]
    heading = scene.get("heading") or sid
    desc = scene.get("visual", {}).get("description") or ""
    narr = scene.get("narration") or ""
    blob_low = f"{sid} {heading} {desc} {narr}".lower()
    s_start = float(scene.get("start", 0) if actual_start is None else actual_start)
    s_dur = float((scene.get("end", 5) - scene.get("start", 0)) if actual_dur is None else actual_dur)
    if component != "semantic-artwork":
        s_dur = max(1.2, s_dur)
    storyboard = compile_scene_storyboard(
        scene,
        component,
        scene_words or [],
        s_start,
        s_dur,
        brief=brief,
        assignment=assignment,
    )

    colors = {**style.get("tokens", {}), **brand.get("colors", {})}
    accent = colors.get("brand-accent") or "#ea580c"
    secondary = colors.get("brand-accent-bright") or colors.get("brand-secondary") or "#4350d8"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = "#ea580c"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", secondary):
        secondary = "#4350d8"

    heading_html = f'<h1 class="vrs-auto-heading">{_format_heading_html(heading)}</h1>'
    cursor_svg = (
        f'<svg class="vrs-cursor-ptr" id="cur-{escape(sid)}" viewBox="0 0 36 36">'
        '<path d="M4 3 L14 31 L18 20 L29 17 Z" fill="#181412" stroke="#ffffff" stroke-width="2.5" stroke-linejoin="round"/></svg>'
    )
    extra_js = ""
    is_git_ctx = any(k in blob_low for k in ("git", "github", "commit", "repository", "pull request"))

    if component == "semantic-artwork":
        body = _artwork_markup(storyboard)
    elif component == "comparison_split":
        entities = [str(e).strip() for e in (storyboard.get("entities") or []) if str(e).strip()]
        lbl_left, lbl_right = _pick_summary_labels(f"{heading}. {desc}")
        if "git ≠" in heading.lower() or ("git" in heading.lower() and "github" in blob_low and "version control" not in heading.lower()):
            lbl_left, lbl_right = "Git", "GitHub"
        elif len(entities) >= 2 and lbl_left.lower() == "primary concept":
            lbl_left, lbl_right = entities[0], entities[1]
        op_char = "≠" if any(k in blob_low for k in ("≠", "not equal", "not-equal", "different", "alag", "nahi", "vs")) else "⇄"
        slug_left = _short_label(lbl_left, 18)
        slug_right = _short_label(lbl_right, 18)
        tag_left = "LOCAL TOOL" if is_git_ctx else "APPROACH A"
        tag_right = "CLOUD PLATFORM" if is_git_ctx else "APPROACH B"
        note_left = "Offline · Track History" if is_git_ctx else "Local Baseline"
        note_right = "Online Host · Team Share" if is_git_ctx else "Shared Cloud Sync"
        stamp_txt = "SAME NAHI!" if ("nahi" in blob_low or "alag" in blob_low) else "NOT THE SAME!"
        outcome_txt = "✓ Connection Clear · Coding = Easy" if ("easy" in blob_low or "hinglish" == (brief.get("language") or "").lower()) else "✓ Connected Workflow · Verified"
        left_emblem = _subject_emblem_svg(lbl_left, accent, secondary)
        right_emblem = _subject_emblem_svg(lbl_right, accent, secondary)
        body = f"""
        <div class="vrs-auto-stage" style="justify-content:flex-start;gap:22px;position:relative">
          <div style="position:relative;display:grid;grid-template-columns:1fr 1fr;gap:24px;height:418px;align-items:stretch">
            <div class="vrs-emblem-card" id="cmp-left-{escape(sid)}" data-ev="left-card">
              <span class="vrs-pill vrs-pill--warm" style="font-size:18px;padding:5px 14px">{escape(tag_left)}</span>
              {left_emblem}
              <div>
                <div style="font-size:40px;font-weight:800;line-height:1.1;color:#181412">{escape(slug_left)}</div>
                <div style="font-size:21px;font-weight:600;color:#57534e;margin-top:6px">{escape(note_left)}</div>
              </div>
            </div>

            <div class="vrs-emblem-card" id="cmp-right-{escape(sid)}" data-ev="right-card">
              <span class="vrs-pill vrs-pill--indigo" style="font-size:18px;padding:5px 14px">{escape(tag_right)}</span>
              {right_emblem}
              <div>
                <div style="font-size:40px;font-weight:800;line-height:1.1;color:#181412">{escape(slug_right)}</div>
                <div style="font-size:21px;font-weight:600;color:#57534e;margin-top:6px">{escape(note_right)}</div>
              </div>
            </div>

            <div data-ev="verdict-stamp" data-keep-visible="1" data-recoil-left="#cmp-left-{escape(sid)}" data-recoil-right="#cmp-right-{escape(sid)}" style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:flex;flex-direction:column;align-items:center;gap:14px;z-index:12;pointer-events:none">
              <div class="vrs-op-circle" data-confirm-swap="{escape(op_char)}" style="width:88px;height:88px;font-size:46px;border-width:5px">=</div>
              <div class="vrs-rubber-stamp" style="color:#c2410c;background:#fff7ed;font-size:21px;padding:7px 18px;box-shadow:0 14px 28px rgba(194,65,12,0.22)">{escape(stamp_txt)}</div>
            </div>
          </div>

          <div class="vrs-card" data-ev="bridge-connector" style="flex:1;padding:24px 28px;justify-content:space-between;position:relative">
            <div style="display:flex;align-items:center;justify-content:space-between">
              <span style="font-size:24px;font-weight:800;color:#181412">How Both Work Together</span>
              <span class="vrs-pill vrs-pill--warm" style="font-size:18px;padding:5px 14px">LIVE PIPELINE</span>
            </div>

            <div style="position:relative;display:grid;grid-template-columns:220px 1fr 220px;align-items:center;gap:10px;padding:12px 0">
              <div style="background:#fff7ed;border:1.5px solid #fed7aa;border-radius:18px;padding:16px;text-align:center">
                <div class="vrs-mono" style="font-size:17px;font-weight:800;color:#c2410c">STEP 01 · LOCAL</div>
                <div style="font-size:25px;font-weight:800;color:#181412;margin-top:4px">{escape(slug_left)}</div>
              </div>

              <div style="position:relative;height:86px;display:flex;align-items:center;justify-content:center">
                <svg viewBox="0 0 380 86" style="width:100%;height:86px;overflow:visible">
                  <path d="M10 56 C110 10, 270 10, 370 56" stroke="#e5dbd0" stroke-width="5" fill="none"/>
                  <path d="M10 56 C110 10, 270 10, 370 56" stroke="{accent}" stroke-width="5" stroke-dasharray="10 7" fill="none"/>
                  <circle cx="10" cy="56" r="8" fill="{accent}"/>
                  <circle cx="370" cy="56" r="8" fill="#059669"/>
                </svg>
                <div class="vrs-pkt js-fly-pkt" data-pkt-x0="-130" data-pkt-x1="130" data-pkt-y0="6" data-pkt-arc="-28" style="position:absolute;font-size:16px;padding:6px 12px">sync ▸</div>
              </div>

              <div style="background:#eef2ff;border:1.5px solid #c7d2fe;border-radius:18px;padding:16px;text-align:center">
                <div class="vrs-mono" style="font-size:17px;font-weight:800;color:{secondary}">STEP 02 · CLOUD</div>
                <div style="font-size:25px;font-weight:800;color:#181412;margin-top:4px">{escape(slug_right)}</div>
              </div>
            </div>

            <div class="vrs-diff-line vrs-diff-add" data-ev="benefit-pill" style="justify-content:center;font-size:23px;padding:15px 20px;border-radius:16px;border:1.5px solid #86efac">
              {escape(outcome_txt)}
            </div>
          </div>
          {cursor_svg}
        </div>"""

    elif component == "editor_history":
        cmds = _extract_commands(scene)
        bullets = _extract_bullets(scene, 2)
        c_lbl = cmds[0] if (cmds and cmds[0].lower() != heading.lower()) else "app.js"
        editor_pane = f"""<div class="vrs-code-pane">
              <div class="vrs-code-line"><span class="vrs-code-ln">12</span><span>export function <strong style="color:{secondary}">saveVersion</strong>(state) {{</span></div>
              <div class="vrs-code-line" style="color:#78716c"><span class="vrs-code-ln">13</span><span>  const snapshot = clone(state);</span></div>
              <div class="vrs-diff-line vrs-diff-add" data-ev="code-change">
                <span class="vrs-code-ln" style="color:#059669">+14</span>
                <span class="vrs-type-clip">return commit(snapshot, "track change");</span>
              </div>
              <div class="vrs-code-line"><span class="vrs-code-ln">15</span><span>}}</span></div>
            </div>"""
        editor_win = _window_box(
            f"editor · {escape(_short_label(c_lbl, 24))} · main",
            editor_pane,
            badge_html='<span class="vrs-pill vrs-pill--warm" style="font-size:19px;padding:5px 14px">VERSION CONTROL</span>',
            box_attrs='data-ev="editor-window"',
        )
        body = f"""
        <div class="vrs-auto-stage" style="gap:24px;position:relative">
          {editor_win}

          <div class="vrs-card" data-ev="history-commits" style="padding:24px 26px;gap:18px">
            <div style="display:flex;align-items:center;justify-content:space-between">
              <span style="font-size:24px;font-weight:800;color:#181412">Tracked Commit History</span>
              <span class="vrs-pill vrs-pill--success" style="font-size:19px;padding:6px 14px">✓ 3 SNAPSHOTS SAVED</span>
            </div>
            <svg viewBox="0 0 860 66" style="width:100%;height:66px;overflow:visible">
              <path class="js-draw" d="M84 33 H776" stroke="#d6cbc0" stroke-width="5" stroke-linecap="round" fill="none"/>
              <circle cx="84" cy="33" r="16" fill="#059669" stroke="#ffffff" stroke-width="4"/>
              <circle cx="430" cy="33" r="16" fill="{secondary}" stroke="#ffffff" stroke-width="4"/>
              <circle cx="776" cy="33" r="18" fill="{accent}" stroke="#ffffff" stroke-width="4"/>
            </svg>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px">
              <div style="background:#f0fdf4;border:1.5px solid #bbf7d0;border-radius:16px;padding:14px">
                <div class="vrs-mono" style="font-size:18px;font-weight:800;color:#059669">v1 · 4a91f</div>
                <div style="font-size:21px;font-weight:700;margin-top:4px">Initial Code</div>
              </div>
              <div style="background:#eef2ff;border:1.5px solid #c7d2fe;border-radius:16px;padding:14px">
                <div class="vrs-mono" style="font-size:18px;font-weight:800;color:{secondary}">v2 · 8c20d</div>
                <div style="font-size:21px;font-weight:700;margin-top:4px">+ Code Change</div>
              </div>
              <div style="background:#fff7ed;border:1.5px solid #fed7aa;border-radius:16px;padding:14px">
                <div class="vrs-mono" style="font-size:18px;font-weight:800;color:{accent}">v3 · 9e12b</div>
                <div style="font-size:21px;font-weight:700;margin-top:4px">Safe History ✓</div>
              </div>
            </div>
            <div style="font-size:22px;font-weight:600;color:#57534e">{escape(bullets[0] if bullets else heading)}</div>
          </div>
          {cursor_svg}
        </div>"""

    elif component == "history_snapshots":
        labels = _extract_quoted_labels(desc, 3)
        while len(labels) < 3:
            labels.append(["Working UI", "+ New Feature", "Broken UI"][len(labels)])
        has_err = any(k in blob_low for k in ("broken", "bigad", "error", "bug", "fail"))
        ide_pane = f"""<div style="padding:18px 20px;display:flex;flex-direction:column;gap:12px;flex:1" class="vrs-mono">
                <div style="font-size:19px;color:#d6d3d1"><span style="color:#78716c">01</span> const ui = initApp();</div>
                <div style="font-size:19px;color:#d6d3d1"><span style="color:#78716c">02</span> export function render() {{</div>
                <div data-ev="v2-feature" data-restored-dim style="font-size:18px;padding:8px 12px" class="vrs-diff-line vrs-diff-add">
                  <span class="vrs-type-clip">+03 const f = featureV2();</span>
                </div>
                <div style="position:relative;height:96px;border-radius:12px;overflow:hidden">
                  <div data-ok-layer style="position:absolute;inset:0;padding:12px;background:rgba(255,255,255,0.04);border-left:4px solid #10b981;font-size:18px;color:#e7e5e4;display:flex;flex-direction:column;justify-content:center;gap:4px">
                    <div>04  return renderUI(ui);</div>
                    <div style="font-size:16px;color:#34d399">// Status: 200 OK</div>
                  </div>
                  <div data-broken-layer data-ev="v3-broken" style="position:absolute;inset:0;padding:12px;background:#450a0a;border-left:4px solid #ef4444;font-size:18px;color:#fecaca;display:flex;flex-direction:column;justify-content:center;gap:4px;opacity:0">
                    <div>-04 return f.draw(null);</div>
                    <div style="font-size:16px;color:#fca5a5;font-weight:800">✕ TypeError: App crashed!</div>
                  </div>
                  <div data-restored-layer style="position:absolute;inset:0;padding:12px;background:#064e3b;border-left:4px solid #34d399;font-size:18px;color:#ecfdf5;display:flex;flex-direction:column;justify-content:center;gap:4px;opacity:0">
                    <div>✓04 return renderUI(ui);</div>
                    <div style="font-size:16px;color:#6ee7b7;font-weight:800">✓ Snapshot v1 restored</div>
                  </div>
                </div>
                <div style="font-size:19px;color:#d6d3d1"><span style="color:#78716c">05</span> }}</div>
              </div>"""
        ide_win = _window_box(
            "app.js · main", ide_pane,
            badge_html='<span class="vrs-pill vrs-pill--dark" style="font-size:17px;padding:4px 12px">IDE</span>',
            dark=True, box_attrs='data-ev="editor-window"',
        )
        live_pane = f"""<div style="position:relative;flex:1;padding:18px;display:flex;flex-direction:column;gap:12px;background:#fcfaf7">
                <div style="display:flex;align-items:center;justify-content:space-between">
                  <span style="font-size:21px;font-weight:800;color:#181412">Live App</span>
                  <span class="vrs-pill vrs-pill--success" style="font-size:16px;padding:4px 10px">● 200 OK</span>
                </div>
                <div style="height:54px;border-radius:14px;background:#ffffff;border:1.5px solid #e5dbd0;display:flex;align-items:center;padding:0 14px;font-size:18px;font-weight:700">✓ Core Dashboard</div>
                <div data-ev-after="v2-feature" style="height:54px;border-radius:14px;background:#eef2ff;border:1.5px solid #c7d2fe;display:flex;align-items:center;padding:0 14px;font-size:18px;font-weight:700;color:#3730a3">✨ New Feature v2</div>
                <div style="height:64px;border-radius:14px;background:#f4efe9;border:1.5px dashed #d6cbc0"></div>

                <div data-broken-layer style="position:absolute;inset:0;background:rgba(254,226,226,0.96);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:18px;opacity:0">
                  <div style="font-size:52px;line-height:1">⚠️</div>
                  <div style="font-size:28px;font-weight:900;color:#991b1b">APP CRASHED</div>
                  <div class="vrs-rubber-stamp" data-broken-stamp style="color:#dc2626;background:#ffffff;font-size:20px">{"BROKEN !" if has_err else "ERROR !"}</div>
                </div>

                <div data-restored-layer style="position:absolute;inset:0;background:#f0fdf4;padding:18px;display:flex;flex-direction:column;gap:12px;opacity:0">
                  <div style="display:flex;align-items:center;justify-content:space-between">
                    <span style="font-size:21px;font-weight:800;color:#065f46">Live App</span>
                    <span class="vrs-pill vrs-pill--success" style="font-size:16px;padding:4px 10px">✓ 200 OK</span>
                  </div>
                  <div style="height:54px;border-radius:14px;background:#ffffff;border:1.5px solid #86efac;display:flex;align-items:center;padding:0 14px;font-size:18px;font-weight:800;color:#065f46">✓ Core Dashboard Clean</div>
                  <div style="height:54px;border-radius:14px;background:#dcfce7;border:1.5px solid #86efac;display:flex;align-items:center;padding:0 14px;font-size:18px;font-weight:700;color:#065f46">✓ Running v1 (4a91f0)</div>
                </div>
              </div>"""
        live_win = _window_box("localhost:3000", live_pane, box_attrs='data-shake-target style="position:relative"')
        body = f"""
        <div class="vrs-auto-stage" style="justify-content:flex-start;gap:20px;position:relative">
          <div style="display:grid;grid-template-columns:1.14fr 0.86fr;gap:18px;height:468px">
            {ide_win}
            {live_win}
          </div>

          <div class="vrs-card" data-ev="history-drawer" style="flex:1;padding:20px 24px;justify-content:space-between;position:relative">
            <div style="display:flex;align-items:center;justify-content:space-between">
              <span style="font-size:24px;font-weight:800;color:#181412">Git Commit History · Select Snapshot</span>
              <span class="vrs-pill vrs-pill--warm" style="font-size:17px;padding:5px 12px">COMMIT LOG</span>
            </div>

            <div data-ev="rollback-arc">
              <svg viewBox="0 0 840 72" style="width:100%;height:72px;overflow:visible">
                <path d="M140 48 H700" stroke="#d6cbc0" stroke-width="5" stroke-linecap="round" fill="none"/>
                <path d="M700 48 C700 2, 140 2, 140 48" stroke="#059669" stroke-width="6" stroke-dasharray="10 7" fill="none"/>
                <circle cx="140" cy="48" r="14" fill="#059669" stroke="#ffffff" stroke-width="4"/>
                <circle cx="420" cy="48" r="13" fill="{secondary}" stroke="#ffffff" stroke-width="4"/>
                <circle cx="700" cy="48" r="14" fill="#dc2626" stroke="#ffffff" stroke-width="4"/>
              </svg>
            </div>

            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px">
              <div id="rest-btn-{escape(sid)}" data-ev="cursor-select-v1" data-active-text="✓ v1 · RESTORED" data-active-bg="#059669" style="position:relative;border:2px solid #86efac;background:#f0fdf4;border-radius:16px;padding:14px 16px;cursor:pointer">
                <div class="vrs-click-ring"></div>
                <div data-click-label class="vrs-mono" style="font-size:18px;font-weight:800;color:#059669">v1 · 4a91f0</div>
                <div style="font-size:21px;font-weight:800;margin-top:4px">{escape(_short_label(labels[0], 18))}</div>
              </div>
              <div style="border:1.5px solid #c7d2fe;background:#eef2ff;border-radius:16px;padding:14px 16px">
                <div class="vrs-mono" style="font-size:18px;font-weight:800;color:{secondary}">v2 · 8c20d4</div>
                <div style="font-size:21px;font-weight:800;color:#181412;margin-top:4px">{escape(_short_label(labels[1], 18))}</div>
              </div>
              <div style="border:1.5px solid #fca5a5;background:#fef2f2;border-radius:16px;padding:14px 16px">
                <div class="vrs-mono" style="font-size:18px;font-weight:800;color:#dc2626">v3 · 9f02a1</div>
                <div style="font-size:21px;font-weight:800;color:#181412;margin-top:4px">{escape(_short_label(labels[2], 18))} ✕</div>
              </div>
            </div>

            <div class="vrs-diff-line vrs-diff-add" data-ev="restore-banner" style="justify-content:center;font-size:20px;padding:12px 18px;border-radius:14px;border:1.5px solid #86efac">
              ✓ Checked out committed snapshot v1 (4a91f0) · Working state restored
            </div>
          </div>
          {cursor_svg}
        </div>"""

    elif component == "offline_workflow":
        cmds = _extract_commands(scene)
        cmd_line = next((c for c in cmds if c.lower().startswith(("git ", "npm ", "python ", "docker ", "kubectl "))), 'git commit -m "offline save"')
        local_pane = f"""<div style="padding:26px 28px;display:flex;flex-direction:column;gap:18px">
              <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px">
                <div style="background:#faf7f4;border:1.5px solid #e5dbd0;border-radius:16px;padding:16px;font-size:22px;font-weight:700">📁 src/app.js</div>
                <div style="background:#faf7f4;border:1.5px solid #e5dbd0;border-radius:16px;padding:16px;font-size:22px;font-weight:700">📁 styles.css</div>
                <div style="background:#fff7ed;border:1.5px solid #fed7aa;border-radius:16px;padding:16px;font-size:22px;font-weight:800;color:{accent}">📦 .git/ history</div>
              </div>
              <div style="background:#1c1917;color:#fafaf9;border-radius:16px;padding:18px 22px;display:flex;align-items:center;justify-content:space-between;gap:14px">
                <div class="vrs-mono" style="font-size:24px"><span style="color:#fb923c">$</span> {escape(cmd_line)}</div>
                <div class="vrs-rubber-stamp" data-ev="local-save-ok" style="color:#34d399;background:rgba(16,185,129,0.14);font-size:18px;padding:5px 12px">OFFLINE OK ✓</div>
              </div>
            </div>"""
        local_win = _window_box(
            "laptop · ~/projects/my-app (.git)", local_pane,
            badge_html='<span class="vrs-pill vrs-pill--warm" style="font-size:19px;padding:5px 14px">LOCAL DISK</span>',
            box_attrs='data-ev="local-machine" style="width:100%"',
        )
        body = f"""
        <div class="vrs-auto-stage" style="align-items:center;gap:24px;position:relative">
          <div class="vrs-toggle-track" data-ev="wifi-toggle-off" style="align-self:center">
            {_icon_svg("wifi_off", accent, secondary).replace('width="78" height="78"', 'width="36" height="36"')}
            <div class="vrs-toggle-switch"><div class="vrs-toggle-knob"></div></div>
            <span class="vrs-mono" data-off-text="WI-FI: OFF" style="font-size:22px;font-weight:800;color:#059669">WI-FI: ON</span>
          </div>

          {local_win}

          <div class="vrs-card" data-ev="optional-account" style="width:88%;padding:18px 26px;flex-direction:row;align-items:center;justify-content:space-between;background:#f8f4ef;border-style:dashed">
            <div style="display:flex;align-items:center;gap:12px">
              <span style="font-size:24px">☁️</span>
              <span style="font-size:24px;font-weight:700;color:#57534e;text-decoration:line-through">GitHub Account Required</span>
            </div>
            <span class="vrs-pill vrs-pill--warm" style="font-size:19px">OPTIONAL · NO ACCOUNT NEEDED</span>
          </div>
          {cursor_svg}
        </div>"""

    elif component == "platform_browser":
        labels = _extract_quoted_labels(desc, 4)
        bullets = _extract_bullets(scene, 3)
        win_title = labels[0] if labels else "creator-project"
        r1 = labels[1] if len(labels) > 1 else bullets[0]
        r2 = labels[2] if len(labels) > 2 else (bullets[1] if len(bullets) > 1 else "Structured modules")
        r3 = labels[3] if len(labels) > 3 else "Public URL & instant sharing"
        browser_inner = f"""<div style="display:flex;gap:18px;padding:14px 24px;background:#faf7f4;border-bottom:1.5px solid #e5dbd0;font-size:20px;font-weight:700;color:#57534e">
              <span style="color:{accent};border-bottom:3px solid {accent};padding-bottom:4px">&lt;/&gt; Code</span>
              <span>Branches · 4</span>
              <span>Pull Requests · 3</span>
              <span style="margin-left:auto;color:#059669">● Online Platform</span>
            </div>
            <div style="padding:22px 24px;display:flex;flex-direction:column;gap:14px" data-ev="repo-files">
              <div style="display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-radius:16px;border:1.5px solid #e5dbd0;background:#ffffff">
                <div style="display:flex;flex-direction:column;gap:4px">
                  <span style="font-size:26px;font-weight:800">📁 {escape(r1)}</span>
                  <span class="vrs-mono" style="font-size:18px;color:#78716c">feat: modular source · hosted online</span>
                </div>
                <span class="vrs-pill vrs-pill--indigo" style="font-size:18px">HOSTED</span>
              </div>
              <div style="display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-radius:16px;border:1.5px solid #e5dbd0;background:#ffffff">
                <div style="display:flex;flex-direction:column;gap:4px">
                  <span style="font-size:26px;font-weight:800">📄 {escape(r2)}</span>
                  <span class="vrs-mono" style="font-size:18px;color:#78716c">README.md · documentation ready</span>
                </div>
                <span class="vrs-pill" style="font-size:18px">PUBLIC</span>
              </div>
              <div style="display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-radius:16px;border:1.5px solid #86efac;background:#f0fdf4">
                <div style="display:flex;flex-direction:column;gap:4px">
                  <span style="font-size:26px;font-weight:800">🌐 {escape(r3)}</span>
                  <span class="vrs-mono" style="font-size:18px;color:#065f46">Shareable cloud workspace · 24/7</span>
                </div>
                <span class="vrs-pill vrs-pill--success" style="font-size:18px">ACCESSIBLE ✓</span>
              </div>
            </div>
            {cursor_svg}"""
        browser_win = _window_box(
            f"https://github.com/creator/{escape(win_title.lower().replace(' ', '-'))}",
            browser_inner,
            badge_html='<span class="vrs-pill vrs-pill--accent" data-ev="share-button" data-active-text="✓ Link Shared" data-active-bg="#059669" style="font-size:19px;padding:6px 15px">↗ Share Repo</span>',
            box_attrs='data-ev="browser-window" style="position:relative"',
        )
        body = f"""
        <div class="vrs-auto-stage" style="gap:20px;position:relative">
          {browser_win}
        </div>"""

    elif component == "collaboration_network":
        bullets = _extract_bullets(scene, 2)
        labels = _extract_quoted_labels(desc, 3)
        n1 = labels[0] if len(labels) > 0 else "Developer A"
        n2 = labels[1] if len(labels) > 1 else "Developer B"
        n3 = labels[2] if len(labels) > 2 else "Pull Request #48"
        body = f"""
        <div class="vrs-auto-stage" style="position:relative;justify-content:space-between;padding:8px 0">
          <svg data-ev="branch-review" viewBox="0 0 944 880" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:0">
            <path d="M230 220 C230 330, 472 300, 472 395" stroke="{accent}" stroke-width="5" stroke-dasharray="10 6" fill="none"/>
            <path d="M714 220 C714 330, 472 300, 472 395" stroke="{secondary}" stroke-width="5" stroke-dasharray="10 6" fill="none"/>
            <path d="M472 645 V750" stroke="#059669" stroke-width="6" fill="none"/>
          </svg>
          <div data-ev="contributor-nodes" style="display:grid;grid-template-columns:1fr 1fr;gap:22px;position:relative;z-index:2">
            <div class="vrs-card" style="padding:20px 22px;gap:10px">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <span class="vrs-pill vrs-pill--warm" style="font-size:18px;padding:5px 12px">DEV 01</span>
                <span class="vrs-mono" style="font-size:17px;color:#78716c">feat/ui</span>
              </div>
              <div style="font-size:28px;font-weight:800;color:#181412">{escape(n1[:22])}</div>
              <div class="vrs-mono" style="font-size:18px;color:#059669">+ pushed 3 commits</div>
            </div>
            <div class="vrs-card" style="padding:20px 22px;gap:10px">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <span class="vrs-pill vrs-pill--indigo" style="font-size:18px;padding:5px 12px">DEV 02</span>
                <span class="vrs-mono" style="font-size:17px;color:#78716c">fix/api</span>
              </div>
              <div style="font-size:28px;font-weight:800;color:#181412">{escape(n2[:22])}</div>
              <div class="vrs-mono" style="font-size:18px;color:{secondary}">✓ code review approved</div>
            </div>
          </div>

          <div class="vrs-card" style="align-self:center;width:90%;padding:24px 28px;position:relative;z-index:2;gap:14px;border-color:{secondary}">
            <div style="display:flex;align-items:center;justify-content:space-between">
              <span class="vrs-mono" style="font-size:20px;font-weight:800;color:{secondary}">PULL REQUEST #48</span>
              <span class="vrs-pill vrs-pill--success" style="font-size:18px;padding:5px 12px">Checks 3/3 ✓</span>
            </div>
            <div style="font-size:34px;font-weight:800;color:#181412">{escape(n3[:28])}</div>
            <div style="display:flex;justify-content:space-between;align-items:center;gap:12px">
              <span style="font-size:20px;color:#57534e">0 conflicts · Ready for main</span>
              <span class="vrs-pill vrs-pill--accent" data-ev="merge-pr-btn" data-active-text="✓ Merged PR #48" data-active-bg="#059669" style="font-size:20px">Merge PR</span>
            </div>
          </div>

          <div class="vrs-card" style="padding:20px 26px;flex-direction:row;align-items:center;justify-content:space-between;position:relative;z-index:2;border-color:#86efac;background:#f0fdf4">
            <div style="font-size:25px;font-weight:800;color:#065f46">{escape(bullets[0] if bullets else heading)}</div>
            <span class="vrs-pill vrs-pill--success js-stamp" style="font-size:19px">MAIN SYNCED ✓</span>
          </div>
          {cursor_svg}
        </div>"""

    elif component == "terminal_pipeline":
        cmds = _extract_commands(scene)
        c0 = cmds[0] if len(cmds) > 0 else "git add ."
        c1 = cmds[1] if len(cmds) > 1 else 'git commit -m "feat: release"'
        c2 = cmds[2] if len(cmds) > 2 else "git push origin main"
        cli_inner = f"""<div style="padding:20px 26px;display:flex;flex-direction:column;gap:14px" class="vrs-mono">
              <div style="font-size:22px;color:#a8a29e"><span style="color:#38bdf8">$</span> {escape(c0)} <span style="color:#34d399;font-size:18px">✓ staged</span></div>
              <div>
                <div data-ev="cmd-commit"><span class="vrs-type-clip" style="font-size:25px;color:#fafaf9"><span style="color:#fb923c">$</span> {escape(c1)}</span></div>
                <div data-ev-after="cmd-commit" style="font-size:18px;color:#34d399;margin-top:4px">↳ [main c7a2f1] Snapshot saved to local .git</div>
              </div>
              <div>
                <div data-ev="cmd-push"><span class="vrs-type-clip" style="font-size:25px;color:#fafaf9"><span style="color:#fb923c">$</span> {escape(c2)}</span></div>
                <div data-ev-after="cmd-push" style="font-size:18px;color:#fbbf24;margin-top:4px">↳ Writing objects: 100% (4/4) → origin/main ✓</div>
              </div>
            </div>"""
        cli_win = _window_box(
            "zsh · ~/dev/my-app (main)", cli_inner,
            badge_html='<span class="vrs-pill vrs-pill--dark" style="font-size:17px;padding:4px 12px">CLI LIVE</span>',
            dark=True, box_attrs='style="width:100%"',
        )
        remote_inner = f"""<div style="padding:16px 20px;display:flex;flex-direction:column;gap:11px;flex:1;justify-content:space-between;position:relative">
                <div style="display:flex;align-items:center;gap:10px;font-size:17px;font-weight:700;color:#57534e">
                  <span style="color:{accent}">&lt;/&gt; Code</span><span>·</span><span>main</span>
                  <span class="vrs-rubber-stamp" data-ev="remote-sync-confirm" style="margin-left:auto;color:#059669;background:#ecfdf5;font-size:16px;padding:4px 10px">PUSHED ✓</span>
                </div>
                <div style="position:relative;height:66px">
                  <div data-arrival-hide="remote-sync-confirm" style="position:absolute;inset:0;padding:12px 14px;border-radius:14px;background:#fff7ed;border:1.5px dashed #fb923c;color:#9a3412;font-size:18px;font-weight:700;display:flex;align-items:center">
                    ⏳ Waiting for git push...
                  </div>
                  <div data-arrival-show="remote-sync-confirm" style="position:absolute;inset:0;padding:12px 14px;border-radius:14px;background:#f0fdf4;border:2px solid #10b981;color:#065f46;font-size:18px;font-weight:800;display:flex;align-items:center;justify-content:space-between;opacity:0">
                    <span class="vrs-mono">c7a2f1 · synced</span>
                    <span>ONLINE ✓</span>
                  </div>
                </div>
                <div style="display:flex;flex-direction:column;gap:8px;font-size:17px;color:#44403c" class="vrs-mono">
                  <div style="display:flex;justify-content:space-between;padding:8px 12px;border-radius:10px;background:#faf7f4;border:1px solid #e5dbd0"><span>📁 src/</span><span style="color:#78716c">v1 · main</span></div>
                  <div style="display:flex;justify-content:space-between;padding:8px 12px;border-radius:10px;background:#faf7f4;border:1px solid #e5dbd0"><span>📄 README.md</span><span style="color:#78716c">docs</span></div>
                </div>
                <div style="font-size:18px;font-weight:700;color:#57534e">02 · Live shared repository</div>
              </div>"""
        remote_win = _window_box(
            "github.com/creator/my-app", remote_inner,
            box_attrs='style="position:relative"', url_attrs='style="font-size:16px"', head_style="padding:10px 16px",
        )
        body = f"""
        <div class="vrs-auto-stage" style="justify-content:flex-start;gap:14px;position:relative">
          {cli_win}

          <div data-ev="push-transfer-arc" style="position:relative;height:96px;display:flex;align-items:center;justify-content:center">
            <svg viewBox="0 0 900 96" style="width:100%;height:96px;overflow:visible">
              <path d="M210 82 C340 8, 560 8, 690 82" stroke="#e5dbd0" stroke-width="5" fill="none"/>
              <path d="M210 82 C340 8, 560 8, 690 82" stroke="{accent}" stroke-width="6" stroke-dasharray="11 7" fill="none"/>
              <circle cx="210" cy="82" r="9" fill="#059669"/>
              <circle cx="690" cy="82" r="9" fill="{accent}"/>
            </svg>
            <div class="vrs-pkt js-fly-pkt" data-pkt-x0="-240" data-pkt-x1="240" data-pkt-y0="22" data-pkt-arc="-46" style="position:absolute">● c7a2f1</div>
            <div class="vrs-pkt js-fly-pkt" data-pkt-x0="-240" data-pkt-x1="240" data-pkt-y0="22" data-pkt-arc="-46" style="position:absolute;background:linear-gradient(135deg,#059669,#10b981)">● 8c20d4</div>
          </div>

          <div style="flex:1;display:grid;grid-template-columns:0.94fr 1.06fr;gap:20px;align-items:stretch">
            <div class="vrs-card" style="padding:20px 22px;justify-content:space-between;background:#fcfaf7">
              <div style="display:flex;align-items:center;justify-content:space-between">
                <span style="font-size:23px;font-weight:800;color:#181412">💻 Local Repo</span>
                <span class="vrs-pill vrs-pill--warm" style="font-size:16px;padding:4px 10px">.git</span>
              </div>
              <div style="display:flex;flex-direction:column;gap:10px" class="vrs-mono">
                <div style="padding:10px 14px;border-radius:12px;background:#ffffff;border:1.5px solid #e5dbd0;font-size:17px;color:#57534e">4a91f0 · init core app</div>
                <div style="padding:10px 14px;border-radius:12px;background:#ffffff;border:1.5px solid #e5dbd0;font-size:17px;color:#57534e">8c20d4 · layout setup</div>
                <div data-ev="local-repo-card" style="padding:12px 14px;border-radius:14px;background:#f0fdf4;border:2px solid #10b981;font-size:18px;font-weight:800;color:#065f46;display:flex;align-items:center;justify-content:space-between">
                  <span>● c7a2f1 feat</span>
                  <span style="font-size:15px">SAVED ✓</span>
                </div>
              </div>
              <div style="font-size:18px;font-weight:700;color:#059669">01 · Stored on your laptop</div>
            </div>

            {remote_win}
          </div>
          {cursor_svg}
        </div>"""

    elif component == "folder_workspace":
        labels = _extract_quoted_labels(desc, 4)
        f1 = labels[0] if len(labels) > 0 else "raw-footage"
        f2 = labels[1] if len(labels) > 1 else "style-reference"
        f3 = labels[2] if len(labels) > 2 else "export"
        body = f"""
        <div class="vrs-auto-stage" style="align-items:center;gap:24px;position:relative">
          <div class="vrs-card" data-ev="dropzone-card" style="width:100%;padding:28px;border:2px dashed {accent};background:#fffaf5;align-items:center;gap:14px">
            <div style="font-size:30px;font-weight:800">📂 Project Workspace Root</div>
            <span class="vrs-pill vrs-pill--accent" data-ev="folder-expand-btn" data-active-text="✓ Folders Ready" data-active-bg="#059669">Create Project Folders</span>
          </div>
          <div data-ev="workspace-tree" style="width:100%;display:grid;grid-template-columns:repeat(3,1fr);gap:18px">
            <div class="vrs-card" style="padding:22px;gap:8px;align-items:center;text-align:center"><span style="font-size:42px">🎬</span><strong style="font-size:22px">{escape(f1)}</strong><span class="vrs-mono" style="font-size:17px;color:#78716c">Input Clips</span></div>
            <div class="vrs-card" style="padding:22px;gap:8px;align-items:center;text-align:center"><span style="font-size:42px">🎨</span><strong style="font-size:22px">{escape(f2)}</strong><span class="vrs-mono" style="font-size:17px;color:#78716c">Design Tokens</span></div>
            <div class="vrs-card" style="padding:22px;gap:8px;align-items:center;text-align:center"><span style="font-size:42px">🚀</span><strong style="font-size:22px">{escape(f3)}</strong><span class="vrs-mono" style="font-size:17px;color:#78716c">Final MP4</span></div>
          </div>
          {cursor_svg}
        </div>"""

    elif component == "search_serp":
        cmds = _extract_commands(scene)
        query_txt = cmds[0] if cmds else heading
        bullets = _extract_bullets(scene, 2)
        not_found = any(k in blob_low for k in ("not found", "missing", "hide", "lose"))
        serp_inner = f"""<div style="padding:24px;display:flex;flex-direction:column;gap:14px" data-ev="serp-results">
              <div style="padding:16px 20px;border-radius:16px;border:1.5px solid #e5dbd0;background:#faf7f4">
                <div style="font-size:18px;color:{secondary};font-weight:700">1. {escape(bullets[0] if bullets else heading)}</div>
                <div style="font-size:24px;font-weight:800;margin-top:4px">Verified Result Row</div>
              </div>
              <div style="padding:16px 20px;border-radius:16px;border:1.5px solid #e5dbd0;background:#ffffff;display:flex;align-items:center;justify-content:space-between">
                <div>
                  <div style="font-size:18px;color:#78716c;font-weight:700">2. Search Index Status</div>
                  <div style="font-size:24px;font-weight:800;margin-top:4px">{escape(bullets[1] if len(bullets) > 1 else "Page visibility check")}</div>
                </div>
                <div class="vrs-rubber-stamp" data-ev="serp-stamp" style="color:{"#dc2626" if not_found else "#059669"};background:#ffffff">{"NOT FOUND" if not_found else "VERIFIED ✓"}</div>
              </div>
            </div>"""
        serp_win = _window_box(
            f'<span class="vrs-type-clip">🔍 {escape(_short_label(query_txt, 34))}</span>',
            serp_inner, url_attrs='data-ev="search-query"',
        )
        body = f"""
        <div class="vrs-auto-stage" style="gap:20px;position:relative">
          {serp_win}
          {cursor_svg}
        </div>"""

    elif component in ("three_workstation", "three_node_offline"):
        offline = (component == "three_node_offline")
        labels = _extract_quoted_labels(desc, 4)
        cmds = _extract_commands(scene)
        tag_a = _short_label(labels[0], 20) if labels else ("AIR-GAPPED LOCAL" if offline else "3D WORKSPACE")
        tag_b = _short_label(labels[1], 20) if len(labels) > 1 else ("ZERO INTERNET REQ" if offline else "CLOUD SYNC READY")
        cmd_tag = cmds[0] if (cmds and cmds[0].lower() != heading.lower()) else "git commit -m 'v1'"
        pill_top = (
            f'<span class="vrs-pill vrs-pill--danger" id="wifi-{escape(sid)}">WI-FI OFF</span>'
            if offline else
            f'<span class="vrs-pill vrs-pill--dark">$ {escape(_short_label(cmd_tag, 24))}</span>'
        )
        n1, n2, n3 = ("Working Copy", "Save Locally", "No Upload Needed") if offline else ("Local Disk", "Snapshot History", "Cloud Sync Ready")
        sub1, sub2, sub3 = ("Zero Network", "All Versions Kept", "Ready Offline ✓") if offline else ("Tracked Diffs", "Checkpoint Hash", "Verified ✓")
        body = f"""
        <div class="vrs-auto-stage" style="gap:18px">
          <div class="vrs-three-viewport js-beat" id="three-{escape(sid)}">
            <div class="vrs-three-hud-top">
              {pill_top}
              <span class="vrs-pill vrs-pill--accent">{escape(tag_b)}</span>
            </div>
            <div class="vrs-three-hud-bot">
              <span class="vrs-pill vrs-pill--dark">{escape(tag_a)}</span>
              <span class="vrs-pill vrs-pill--success js-stamp" id="st-{escape(sid)}">{"OFFLINE OK ✓" if offline else "SNAPSHOTTED ✓"}</span>
            </div>
          </div>
          <div class="vrs-tele-dock js-beat">
            <div class="vrs-tele-node"><strong>{escape(n1)}</strong><span>{escape(sub1)}</span></div>
            <div class="vrs-mono" style="font-size:24px;font-weight:800;color:{accent}">→</div>
            <div class="vrs-tele-node"><strong>{escape(n2)}</strong><span>{escape(sub2)}</span></div>
            <div class="vrs-mono" style="font-size:24px;font-weight:800;color:#059669">→</div>
            <div class="vrs-tele-node" style="border-color:#86efac;background:#f0fdf4"><strong>{escape(n3)}</strong><span style="color:#065f46">{escape(sub3)}</span></div>
          </div>
        </div>"""
        extra_js = _three_js_script(sid, "offline" if offline else "workstation", accent, secondary, heading, labels, cmds)

    elif component == "process_timeline":
        steps_list = _extract_bullets(scene, 3)
        cards_html = _step_cards_html(steps_list, pad="22px 26px", font_size=28, pill_prefix="STEP ")
        body = f"""
        <div class="vrs-auto-stage" style="gap:20px">
          {cards_html}
          <div style="height:10px;background:#ece4da;border-radius:999px;overflow:hidden"><div class="js-bar" data-width="100%" style="height:100%;background:linear-gradient(90deg,{accent},#059669);border-radius:999px"></div></div>
        </div>"""

    elif component == "metric_showcase":
        nums = re.findall(r"\b(\d+(?:\.\d+)?)\s*(%|kcal|mg|g|x|fps|s|ms|USD|INR)?\b", f"{heading} {narr}")
        bullets = _extract_bullets(scene, 2)
        val_num, val_unit = nums[0] if nums else ("94", "%")
        v2 = f"{nums[1][0]}{nums[1][1]}" if len(nums) > 1 else "2.4x"
        body = f"""
        <div class="vrs-auto-stage" style="gap:22px">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
            <div class="vrs-card" data-ev="metric-kpi-1" style="padding:26px;gap:10px">
              <span class="vrs-pill vrs-pill--warm" style="align-self:flex-start;font-size:18px">MEASURED METRIC</span>
              <div style="font-size:68px;font-weight:900;letter-spacing:-0.03em;color:{accent}" data-count-to="{escape(val_num)}" data-count-suffix="{escape(val_unit)}">{escape(val_num + val_unit)}</div>
              <div style="font-size:24px;font-weight:700;color:#44403c">{escape(bullets[0])}</div>
            </div>
            <div class="vrs-card" style="padding:26px;gap:10px;background:#1c1917;color:#fafaf9;border-color:#322d29">
              <span class="vrs-pill vrs-pill--dark" style="align-self:flex-start;font-size:18px">TARGET IMPACT</span>
              <div style="font-size:68px;font-weight:900;letter-spacing:-0.03em;color:#fbbf24">{escape(v2)}</div>
              <div style="font-size:24px;font-weight:700;color:#d6d3d1">{escape(bullets[1] if len(bullets) > 1 else heading)}</div>
            </div>
          </div>
          <div class="vrs-card" data-ev="metric-gauge" style="padding:24px 28px;flex-direction:row;align-items:center;gap:28px">
            <svg width="168" height="106" viewBox="0 0 168 106" style="flex:none">
              <path d="M 18 90 A 66 66 0 0 1 150 90" stroke="#ece4da" stroke-width="14" stroke-linecap="round" fill="none"/>
              <path class="js-draw" data-pct="0.88" d="M 18 90 A 66 66 0 0 1 150 90" stroke="{accent}" stroke-width="14" stroke-linecap="round" fill="none"/>
            </svg>
            <div style="flex:1;display:flex;flex-direction:column;gap:12px">
              <div style="display:flex;align-items:center;justify-content:space-between">
                <span style="font-size:25px;font-weight:800">Verification Score</span>
                <span class="vrs-pill vrs-pill--success" data-ev="metric-stamp" style="font-size:18px">VERIFIED ✓</span>
              </div>
              <div style="height:12px;background:#ece4da;border-radius:999px;overflow:hidden"><div class="js-bar" data-width="92%" style="height:100%;background:{accent};border-radius:999px"></div></div>
            </div>
          </div>
        </div>"""

    elif component == "smartphone_ui":
        bullets = _extract_bullets(scene, 3)
        labels = _extract_quoted_labels(desc, 3)
        body = f"""
        <div class="vrs-auto-stage" style="gap:18px">
          <div class="vrs-three-viewport js-beat" id="three-{escape(sid)}">
            <div class="vrs-three-hud-top">
              <span class="vrs-pill vrs-pill--dark">MOBILE INTERFACE</span>
              <span class="vrs-pill vrs-pill--accent">LIVE 3D PREVIEW</span>
            </div>
            <div class="vrs-three-hud-bot">
              <span class="vrs-pill vrs-pill--success js-stamp">INTERACTIVE ✓</span>
            </div>
          </div>
          <div class="vrs-tele-dock js-beat" style="grid-template-columns:1fr 1fr">
            <div class="vrs-tele-node"><strong>{escape(bullets[0])}</strong><span>Real-Time App State</span></div>
            <div class="vrs-tele-node" style="border-color:#c7d2fe;background:#eef2ff"><strong>{escape(bullets[1] if len(bullets) > 1 else heading)}</strong><span style="color:#3730a3">Verified Experience</span></div>
          </div>
        </div>"""
        extra_js = _three_js_script(sid, "smartphone", accent, secondary, heading, labels or bullets, bullets)

    elif component == "media_spotlight":
        bullets = _extract_bullets(scene, 2)
        body = f"""
        <div class="vrs-auto-stage" style="justify-content:space-between">
          <div class="vrs-pill-row" data-ev="stage-primary">
            <span class="vrs-pill vrs-pill--accent">{escape(_short_label(heading, 28))}</span>
          </div>
          <div class="vrs-card" data-ev="stage-finish" style="padding:22px 28px;margin-top:auto">
            <div style="font-size:27px;font-weight:800">{escape(bullets[0] if bullets else narr[:90])}</div>
          </div>
        </div>"""

    elif component == "creator_cta_outro":
        owner = brand.get("ownerName") or brand.get("brandName") or "Creator"
        handle_raw = brand.get("instagramUsername") or brand.get("handle") or brand.get("id") or ""
        handle = f"@{handle_raw.lstrip('@')}" if handle_raw else ""
        role = brand.get("profileTitle") or "Digital Creator"
        cta = brand.get("cta") or "Follow for more"
        is_engine_outro = sid in {"outro", "brand-outro"}
        if is_engine_outro:
            body = f"""
            <div class="vrs-auto-stage">
              <div class="vrs-cta-card" style="background:rgba(255,255,255,0.45)">
                <div style="margin-top:auto" class="vrs-pill-row js-beat">
                  <span class="vrs-pill">{_icon_svg(_domain_icon(brief.get("topic", "")), accent, secondary)}</span>
                </div>
              </div>
            </div>"""
        else:
            photo_tag = '<div class="vrs-portrait-ring"><img src="assets/creator-photo.png" alt="" /></div>' if has_creator_photo else ""
            body = f"""
            <div class="vrs-auto-stage">
              <div class="vrs-cta-card" data-ev="creator-profile" style="position:relative">
                {photo_tag}
                <div style="font-size:56px;font-weight:800;line-height:1.1;color:#181412">{escape(owner)}</div>
                <div style="font-size:30px;font-weight:700;color:{accent}">{escape(handle)}</div>
                <div style="font-size:25px;font-weight:600;color:#57534e">{escape(role)}</div>
                <div class="vrs-cta-pill" id="cta-pill-{escape(sid)}" data-ev="follow-cta" data-active-text="✓ Following {escape(handle)}" data-active-bg="#059669" style="margin-top:8px">
                  <span>↑</span><span>{escape(cta)}</span>
                </div>
                {cursor_svg}
              </div>
            </div>"""
            extra_js += '\n  const hdr = document.getElementById("profile-header"); if (hdr) tl.to(hdr, {opacity:0, duration:0.25}, scene.start);'

    else:  # diagram_explainer
        bullets = _extract_bullets(scene, 3)
        icon_kind = _domain_icon(f"{heading} {desc} {narr}")
        nodes = _step_cards_html(bullets, pad="20px 26px", font_size=27, pill_prefix="")
        body = f"""
        <div class="vrs-auto-stage" style="gap:18px">
          <div class="vrs-card" style="padding:22px 26px;flex-direction:row;align-items:center;justify-content:space-between;background:#fff7ed;border-color:#fed7aa">
            <div style="font-size:30px;font-weight:800;color:#181412">{escape(heading[:38])}</div>
            {_icon_svg(icon_kind, accent, secondary).replace('width="84" height="84"', 'width="58" height="58"')}
          </div>
          {nodes}
          <div style="height:10px;background:#ece4da;border-radius:999px;overflow:hidden"><div class="js-bar" data-width="100%" style="height:100%;background:{accent};border-radius:999px"></div></div>
        </div>"""

    layout = "vrs-auto-layout vrs-artwork-layout" if component == "semantic-artwork" else "vrs-auto-layout"
    html_doc = f"""<link rel="stylesheet" href="assets/vrs-auto.css" />
<div class="vrs-auto-scene">
  <div class="vrs-auto-bg"></div>
  <div class="vrs-progress-rail"><div class="vrs-progress-fill"></div></div>
  <div class="vrs-meta-badge"></div>
  <div class="{layout}">
    {heading_html}
    {body}
  </div>
</div>
"""
    js_doc = _common_js_setup(sid, scene.get("animation", []), extra_js, storyboard=storyboard)
    return html_doc, js_doc, storyboard


def _stage_declared_scene_assets(plan: dict, brand: dict, assets_dir: Path, creator_photo: Path | None) -> str:
    font_css = ""
    noto_src = Path("/usr/share/fonts/truetype/noto/NotoSerif-BoldItalic.ttf")
    noto_dst = assets_dir / "fonts" / "NotoSerif-BoldItalic.ttf"
    if noto_src.is_file() and not noto_dst.is_file():
        noto_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(noto_src, noto_dst)
    if noto_dst.is_file():
        font_css += (
            '@font-face{font-family:"Noto Serif";src:url("fonts/NotoSerif-BoldItalic.ttf") '
            'format("truetype");font-weight:700;font-style:italic;}\n'
        )
    if creator_photo and creator_photo.is_file():
        shutil.copyfile(creator_photo, assets_dir / "creator-photo.png")
    for scene in plan.get("scenes", []):
        for rel in scene.get("assets", []):
            rel_norm = str(rel).strip().lstrip("/")
            if not rel_norm or ".." in Path(rel_norm).parts:
                continue
            target = (assets_dir / rel_norm).resolve()
            if not target.is_relative_to(assets_dir.resolve()) or target.is_file():
                continue
            cand = (BRAND_DIR / rel_norm).resolve()
            if cand.is_relative_to(BRAND_DIR.resolve()) and cand.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(cand, target)
    return font_css


def _scene_seed_digest(
    scene: dict,
    brief: dict,
    brand: dict,
    style: dict,
    timing_entry: dict,
    assignment: dict | None,
    mode: str,
    scene_words: list[dict] | None = None,
) -> str:
    choreo_file = Path(__file__).parent / "motion_choreography.py"
    return digest(
        Path(__file__),
        *([choreo_file] if choreo_file.is_file() else []),
        scene,
        {k: brief.get(k) for k in ("topic", "niche", "audience", "language", "brand", "style")},
        brand,
        style,
        timing_entry,
        scene_words or [],
        assignment or {},
        mode,
    )


def validate_visual_sources(
    project: Path,
    plan: dict,
    source_dir: Path,
    assignments: dict,
    cfg: dict | None = None,
) -> dict:
    """Validate required scene files, chronological order, paths, static safety and JS syntax."""
    issues: list[str] = []
    if not source_dir.is_dir():
        return {"ok": False, "issues": [f"Visual folder missing: {source_dir}"], "sceneCount": 0}
    try:
        production_render.source_files(source_dir, plan, assignments)
    except ValueError as err:
        issues.append(str(err))

    cfg = cfg if cfg is not None else load_visual_config(project)
    js_files: list[Path] = []
    for scene in plan.get("scenes", []):
        sid = scene["id"]
        if assignments.get(sid, {}).get("mode") in {"image", "video"}:
            continue
        html_path = source_dir / "scenes" / f"{sid}.html"
        js_path = source_dir / "assets" / f"{sid}.js"
        if html_path.is_file():
            raw_html = html_path.read_text(encoding="utf-8", errors="replace")
            storyboard = scene.get("storyboard", {})
            scene_mode = cfg.get("scenes", {}).get(sid, {}).get("mode", cfg.get("mode", "auto"))
            if scene_mode != "custom":
                if storyboard.get("limitation"):
                    issues.append(f"{sid}: {storyboard['limitation']} Use supplied media or a reviewed custom scene.")
                elif "artwork" in storyboard and ('class="vrs-semantic-artwork"' not in raw_html or not js_path.is_file()):
                    issues.append(f"{sid}: automatic scene is missing its narration-driven artwork. Prepare it again.")
            if FORBIDDEN_EXT_URLS.search(raw_html):
                issues.append(f"{sid}.html references an external network URL.")
            if SECRET_LEAK.search(raw_html):
                issues.append(f"{sid}.html contains sensitive credential patterns.")
            if re.search(r"<audio\b", raw_html, re.I):
                issues.append(f"{sid}.html must not embed voice/audio tags.")
        if js_path.is_file():
            raw_js = js_path.read_text(encoding="utf-8", errors="replace")
            if FORBIDDEN_JS.search(raw_js) or FORBIDDEN_EXT_URLS.search(raw_js):
                issues.append(f"{sid}.js uses forbidden network or dynamic code primitives.")
            if SECRET_LEAK.search(raw_js):
                issues.append(f"{sid}.js contains sensitive credential patterns.")
            js_files.append(js_path)

    for js_path in js_files:
        res = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
        if res.returncode != 0:
            issues.append(f"{js_path.name} failed JavaScript syntax check: {res.stderr.strip()}")

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "checkedJsCount": len(js_files),
        "sceneCount": len(plan.get("scenes", [])),
    }


def complete_visual_prompt(project: Path, plan: dict | None = None, timing: dict | None = None) -> str:
    """Build complete structured external coding-agent visual production prompt chronologically."""
    brief = load(project / "brief.json")
    plan = plan or (load(project / "plan.json") if (project / "plan.json").is_file() else None)
    if not plan:
        return ""
    brand, style = profiles(plan, project=project)
    sponsor_ctx = sponsorship.prompt_context(project, plan)
    assignments = asset_library.saved(project)
    measured_map = {s["id"]: s for s in (timing.get("scenes", []) if timing else [])}
    timing_label = "measured speech timeline" if timing else "estimated brief cuts (alignment not yet run)"

    lines = [
        "# Complete Visual Production Prompt — Viral Reel Studio",
        "",
        "Build or refine reviewed HyperFrames vertical (1080x1920) scene sources matching this specification.",
        "",
        "## Project & Destinations",
        f"- Project ID: `{plan['id']}`",
        f"- Project Directory: `{project}`",
        f"- Target Visuals Root: `{project / 'visuals'}`",
        f"- Topic: {plan['topic']}",
        f"- Niche: {plan['niche']} | Audience: {plan['audience']} | Language: {plan['language']}",
        f"- Timing Authority: {timing_label} (total duration: {timing.get('duration', plan['duration']) if timing else plan['duration']:.2f}s)",
        f"- Brand Profile: {brand.get('ownerName') or brand.get('brandName')} (`{brand.get('id')}`, {brand.get('handle', '')}, CTA: `{brand.get('cta', '')}`)",
        f"- Style Library: `{style.get('id')}` ({style.get('name', '')})",
        "",
        "## Renderer Source Contract & Rules",
        "- Deliver `scenes/<scene-id>.html` fragments and optional `assets/<scene-id>.js` files inside `projects/<id>/visuals/`.",
        "- Do NOT emit standalone `<!doctype>`, `<body>`, `<audio>`, or caption overlays (the engine injects voice, active `#FFD400` captions, `#profile-header`, and Three.js/GSAP).",
        "- Reserve top header zone (`y < 310px`) and bottom caption safe zone (`y > 1430px`); keep primary visuals inside `x: 68..1012`, `y: 326..1410`.",
        "- Use shared paused `window.reelTimeline = window.reelTimeline || gsap.timeline({paused:true});`, read chronological `window.reelScenes`, and use relative `assets/three.module.min.js` + `window.__hf.buildReady` + `hf-seek` for 3D WebGL.",
        "- Keep all assets local inside `visuals/assets/` (never external HTTP URLs or credentials).",
    ]
    if sponsor_ctx:
        lines.extend(["", "## Sponsorship Context", json.dumps(sponsor_ctx, indent=2, ensure_ascii=False)])

    lines.extend(["", "## Chronological Scenes"])
    for idx, scene in enumerate(plan["scenes"], start=1):
        sid = scene["id"]
        actual = measured_map.get(sid)
        s_start = actual["start"] if actual else scene["start"]
        s_end = actual["end"] if actual else scene["end"]
        anim_list = actual.get("animation", scene.get("animation", [])) if actual else scene.get("animation", [])
        inst_map = {a["id"]: a.get("instruction", "") for a in scene.get("animation", [])}
        scene_asset_cfg = assignments.get(sid)
        lines.extend([
            f"",
            f"### {idx}. `{sid}` — {scene['heading']} (`{s_start:.2f}s` -> `{s_end:.2f}s`, dur `{s_end - s_start:.2f}s`)",
            f"- Visual Type: `{scene['visual']['type']}`",
            f"- Clean Narration: {scene['narration']}",
            f"- Visual Description: {scene['visual']['description']}",
            f"- Camera / Transition: {scene['visual'].get('camera', '—')} | {scene['visual'].get('transition', '—')}",
            f"- Scene-Relative Animations ({len(anim_list)}): " + "; ".join(
                f"{a['id']} [{a['start']:.2f}-{a['end']:.2f}s]: {a.get('instruction') or inst_map.get(a['id'], '')}" for a in anim_list
            ),
            f"- Required Scene Assets: {scene.get('assets', [])}",
        ])
        if scene_asset_cfg:
            lines.append(f"- Assigned Library Media ({scene_asset_cfg['mode']}): {json.dumps(scene_asset_cfg['layers'], ensure_ascii=False)}")
        if scene.get("storyboard"):
            lines.append(f"- Storyboard: {json.dumps(scene['storyboard'], ensure_ascii=False)}")

    prompt_text = "\n".join(lines) + "\n"
    write(project / "visual-production-prompt.txt", prompt_text)
    return prompt_text


def _timing_maps(timing: dict | None) -> tuple[dict, dict[str, list[dict]]]:
    measured_map = {s["id"]: s for s in (timing.get("scenes", []) if timing else [])}
    words_by_scene: dict[str, list[dict]] = {}
    for w in (timing.get("words", []) if timing else []):
        words_by_scene.setdefault(w.get("scene", ""), []).append(w)
    return measured_map, words_by_scene


def _resolve_scene_plan(
    plan: dict,
    brief: dict,
    brand: dict,
    style: dict,
    timing: dict | None,
    assignments: dict,
    cfg: dict,
    proj_mode: str,
    *,
    scene_id: str | None = None,
) -> list[dict]:
    measured_map, words_by_scene = _timing_maps(timing)
    used_recently: list[str] = []
    entries: list[dict] = []
    scenes = plan.get("scenes", [])
    for idx, scene in enumerate(scenes):
        sid = scene["id"]
        saved_meta = cfg.get("scenes", {}).get(sid, {})
        if proj_mode == "hybrid" and saved_meta.get("mode") in ("auto", "custom"):
            scene_mode = saved_meta["mode"]
        elif scene_id == sid:
            scene_mode = "auto"
        elif proj_mode == "custom":
            scene_mode = "custom"
        else:
            scene_mode = "auto"
        assign = assignments.get(sid)
        component = select_component(scene, brief, idx, len(scenes), used_recently, assign)
        used_recently.append(component)
        actual = measured_map.get(sid, {"start": scene["start"], "end": scene["end"], "animation": scene.get("animation", [])})
        scene_words = words_by_scene.get(sid, [])
        seed = _scene_seed_digest(scene, brief, brand, style, actual, assign, scene_mode, scene_words)
        entries.append({
            "scene": scene, "id": sid, "savedMeta": saved_meta, "sceneMode": scene_mode,
            "assign": assign, "component": component, "actual": actual,
            "words": scene_words, "seed": seed,
        })
    return entries


def visual_status(project: Path, plan: dict | None, timing: dict | None) -> dict:
    """Compute generation mode, per-scene visual status, staleness and validation state."""
    if not plan:
        return {"mode": "auto", "ready": False, "stale": False, "scenes": [], "validation": {"ok": False, "issues": ["Import a production plan first."]}}
    brief = load(project / "brief.json")
    brand, style = profiles(plan, project=project)
    cfg = load_visual_config(project)
    proj_mode = cfg.get("mode", "auto")
    if proj_mode not in SUPPORTED_MODES:
        proj_mode = "auto"
    source_dir = project / "visuals"
    assignments = asset_library.saved(project)

    scene_rows = []
    any_stale = False
    has_custom = False
    for item in _resolve_scene_plan(plan, brief, brand, style, timing, assignments, cfg, proj_mode):
        scene, sid = item["scene"], item["id"]
        saved_meta, scene_mode, assign = item["savedMeta"], item["sceneMode"], item["assign"]
        suggested_component, actual, seed_now = item["component"], item["actual"], item["seed"]
        html_exists = (source_dir / "scenes" / f"{sid}.html").is_file()
        js_exists = (source_dir / "assets" / f"{sid}.js").is_file()
        asset_mode = (assign or {}).get("mode", "ai")
        if asset_mode in {"image", "video"}:
            status = f"asset-{asset_mode}"
            stale = False
        elif scene_mode == "custom":
            has_custom = True
            status = "custom-ready" if html_exists else "custom-missing"
            stale = False
        else:
            if not html_exists:
                status = "needs-generation"
                stale = False
            elif (saved_meta.get("seedDigest") and saved_meta["seedDigest"] != seed_now
                  or saved_meta.get("status") == "auto_generated" and saved_meta.get("component") != suggested_component):
                status = "stale"
                stale = True
                any_stale = True
            elif saved_meta.get("status") == "auto_generated":
                status = "auto-generated"
                stale = False
            else:
                status = "present"
                stale = False
        scene_rows.append({
            "id": sid,
            "heading": scene["heading"],
            "start": round(actual["start"], 3),
            "end": round(actual["end"], 3),
            "mode": scene_mode,
            "assetMode": asset_mode,
            "component": saved_meta.get("component") or suggested_component,
            "suggestedComponent": suggested_component,
            "hasHtml": html_exists,
            "hasJs": js_exists,
            "status": status,
            "stale": stale,
        })

    val = validate_visual_sources(project, plan, source_dir, assignments, cfg) if source_dir.is_dir() else {"ok": False, "issues": ["Generate visuals or create reviewed HTML sources first."], "sceneCount": 0}
    ready = bool(val["ok"] and timing)
    return {
        "mode": proj_mode,
        "ready": ready,
        "stale": any_stale,
        "hasCustomScenes": has_custom,
        "defaultSource": str(source_dir.relative_to(project.parent.parent)),
        "lastGeneration": cfg.get("lastGeneration"),
        "scenes": scene_rows,
        "validation": val,
        "hasTiming": bool(timing),
    }


def generate_visuals(
    project: Path,
    *,
    mode: str | None = None,
    scene_id: str | None = None,
    force: bool = False,
) -> dict:
    """Automatically generate validated HyperFrames scene HTML, JS, CSS and vendor assets."""
    t0 = time.perf_counter()
    brief = load(project / "brief.json")
    plan = load(project / "plan.json")
    from caption_review import current_caption_state

    timeline, _ = current_caption_state(project, plan)
    timing_is_measured = bool(timeline)
    brand, style = profiles(plan, project=project)
    creator_photo = production_render.profile_assets(brand).get("profile")
    assignments = asset_library.saved(project)

    cfg = load_visual_config(project)
    proj_mode = mode if mode in SUPPORTED_MODES else cfg.get("mode", "auto")
    if proj_mode == "custom" and scene_id is None and not force:
        raise ValueError("Project visual mode is Custom AI. Switch to Automatic or Hybrid mode to auto-generate visuals.")
    cfg["mode"] = proj_mode
    cfg.setdefault("scenes", {})

    active_timing = timeline if timing_is_measured else None
    entries = _resolve_scene_plan(plan, brief, brand, style, active_timing, assignments, cfg, proj_mode, scene_id=scene_id)
    for item in entries:
        if ((scene_id and item["id"] != scene_id)
                or (item["sceneMode"] == "custom" and not force)
                or (item["assign"] or {}).get("mode") in {"image", "video"}):
            continue
        storyboard = item["scene"].get("storyboard", {})
        if storyboard.get("limitation"):
            raise ValueError(f"{item['id']}: {storyboard['limitation']} Use supplied media or a reviewed custom scene.")
        validate_artwork(item["scene"])
        if item["component"] == "semantic-artwork" and active_timing and not item["words"] and storyboard["artwork"].get("events"):
            raise ValueError(f"{item['id']}: approved narration timings are missing for the artwork.")

    source_dir = project / "visuals"
    scenes_dir = source_dir / "scenes"
    assets_dir = source_dir / "assets"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Write shared stylesheet and stage safe project-declared local assets (engine injects gsap/three vendors)
    font_css = _stage_declared_scene_assets(plan, brand, assets_dir, creator_photo)
    body_css = COMMON_CSS if font_css else COMMON_CSS.replace('"Noto Serif", ', '')
    write(assets_dir / "vrs-auto.css", font_css + body_css)

    generated_scenes: list[dict] = []
    for item in entries:
        scene, sid = item["scene"], item["id"]
        saved_meta, scene_mode, assign = item["savedMeta"], item["sceneMode"], item["assign"]
        component, actual, scene_words, seed = item["component"], item["actual"], item["words"], item["seed"]

        if scene_id and sid != scene_id:
            continue
        if scene_mode == "custom" and not force:
            cfg["scenes"][sid] = {**saved_meta, "mode": "custom"}
            continue
        if (not force and scene_id is None and saved_meta.get("status") == "auto_generated"
                and saved_meta.get("seedDigest") == seed and saved_meta.get("component") == component
                and (scenes_dir / f"{sid}.html").is_file()
                and (assets_dir / f"{sid}.js").is_file()):
            continue
        if assign and assign.get("mode") in {"image", "video"}:
            cfg["scenes"][sid] = {"mode": scene_mode, "component": f"asset-{assign['mode']}", "status": f"asset-{assign['mode']}", "seedDigest": seed}
            continue

        actual_start = float(actual["start"])
        actual_dur = float(actual["end"]) - actual_start
        html_doc, js_doc, storyboard = _render_component_html_and_js(
            component,
            scene,
            brief,
            brand,
            style,
            has_creator_photo=bool(creator_photo and creator_photo.is_file()),
            scene_words=scene_words,
            actual_start=actual_start,
            actual_dur=actual_dur,
            assignment=assign,
        )
        write(scenes_dir / f"{sid}.html", html_doc)
        write(assets_dir / f"{sid}.js", js_doc)
        cfg["scenes"][sid] = {
            "mode": "auto",
            "component": component,
            "status": "auto_generated",
            "seedDigest": seed,
            "events": [{"action": e["action"], "target": e["target"], "at": e["at"]} for e in storyboard.get("events", [])],
        }
        generated_scenes.append({"id": sid, "component": component, "start": actual["start"], "end": actual["end"]})

    val = validate_visual_sources(project, plan, source_dir, assignments, cfg)
    elapsed = round(time.perf_counter() - t0, 3)
    cfg["lastGeneration"] = {
        "generatedAt": _now_iso(),
        "elapsedSeconds": elapsed,
        "scenesGenerated": len(generated_scenes),
        "timingSource": "measured" if timing_is_measured else "estimated",
        "validationOk": val["ok"],
    }
    save(_config_path(project), cfg)
    complete_visual_prompt(project, plan, timeline if timing_is_measured else None)
    return {
        "generated": generated_scenes,
        "validation": val,
        "status": visual_status(project, plan, timeline if timing_is_measured else None),
    }


def set_visual_mode(
    project: Path,
    *,
    mode: str | None = None,
    scene_id: str | None = None,
    scene_mode: str | None = None,
    style: str | None = None,
) -> dict:
    plan = load(project / "plan.json")
    cfg = load_visual_config(project)
    if mode is not None:
        if mode not in SUPPORTED_MODES:
            raise ValueError("Choose auto, custom, or hybrid visual production mode.")
        cfg["mode"] = mode
    if style is not None:
        if not isinstance(style, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", style) or not (STYLE_LIB / f"{style}.json").is_file():
            raise ValueError(f"Unknown style profile: {style}")
        cfg["style"] = style
    if scene_id is not None:
        valid_ids = {s["id"] for s in plan.get("scenes", [])}
        if scene_id not in valid_ids:
            raise ValueError(f"Unknown scene ID: {scene_id}")
        if scene_mode not in ("auto", "custom"):
            raise ValueError("Scene visual mode must be auto or custom.")
        cfg.setdefault("scenes", {})[scene_id] = {**cfg.get("scenes", {}).get(scene_id, {}), "mode": scene_mode}
    save(_config_path(project), cfg)
    from caption_review import current_caption_state

    timeline, _ = current_caption_state(project, plan)
    return visual_status(project, plan, timeline)
