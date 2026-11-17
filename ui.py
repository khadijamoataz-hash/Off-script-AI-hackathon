"""Presentation layer for the Foundry web app: brand styles, page layout and the views that turn
agent output into readable cards.

Nothing here decides anything about agents, gates or evidence: it only displays what the
workflow and the evidence store already hold. Headings and subheadings of the analysis are
reproduced exactly as `workflow.py` writes them to `outputs/`.

Pages contain no JavaScript. Pages that update while agents work use a meta refresh.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urlparse

import markdown as markdown_lib

from evidence import Claim, ClaimType, FetchedPage

SLOGAN = "Built to challenge your idea, not flatter it."


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------- styles

CSS = """
:root{color-scheme:light;
--navy:#111126;--purple:#6847F5;--orange:#FF7138;--white:#FFFFFF;--bg:#F7F6FC;--muted:#77758B;
--ink:#111126;--ink-2:#3A3954;--line:#E8E5F3;--line-2:#D8D4EA;
--purple-50:#F3F0FF;--purple-100:#E4DDFE;--purple-700:#4E30D8;
--orange-50:#FFF4EE;--orange-100:#FFDCCB;--orange-700:#B4441A;
--navy-50:#EEEEF4;
--r-lg:20px;--r:16px;--r-sm:12px;
--shadow-sm:0 1px 2px rgba(17,17,38,.05);
--shadow:0 1px 2px rgba(17,17,38,.04),0 10px 28px -16px rgba(17,17,38,.16);
--shadow-lg:0 2px 4px rgba(17,17,38,.04),0 28px 56px -28px rgba(17,17,38,.28);
--font:"Inter",ui-sans-serif,-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@view-transition{navigation:auto}
*{box-sizing:border-box}
html{background:var(--bg);-webkit-text-size-adjust:100%}
body{margin:0;font-family:var(--font);font-size:15.5px;line-height:1.6;color:var(--ink);background:var(--bg);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--purple);text-decoration:none}a:hover{text-decoration:underline;text-underline-offset:3px}
:focus-visible{outline:3px solid rgba(104,71,245,.45);outline-offset:2px}
h1,h2,h3{color:var(--navy);letter-spacing:-.02em;line-height:1.2;margin:0}
h1{font-size:30px;font-weight:800}h2{font-size:21px;font-weight:750}h3{font-size:16.5px;font-weight:700}
p{margin:0 0 12px}p:last-child{margin-bottom:0}
code{font-family:var(--mono);font-size:.84em;background:var(--navy-50);color:var(--ink-2);padding:2px 6px;border-radius:6px;word-break:break-word}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px}
.muted{color:var(--muted)}.small{font-size:13.5px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}

/* header and footer */
.topbar{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.88);backdrop-filter:saturate(1.5) blur(14px);-webkit-backdrop-filter:saturate(1.5) blur(14px);border-bottom:1px solid var(--line)}
.topbar .wrap{display:flex;align-items:center;gap:16px;height:64px}
.brand{display:flex;align-items:center;gap:10px;color:var(--navy);font-weight:800;letter-spacing:.18em;font-size:15px;margin-right:auto}
.brand:hover{text-decoration:none}
.brand-logo{display:block;height:40px;width:auto;max-width:none}
@media(max-width:560px){.brand-logo{height:34px}}
.nav{display:flex;align-items:center;gap:2px}
.nav a{color:var(--ink-2);font-size:14px;font-weight:550;padding:8px 12px;border-radius:9px;white-space:nowrap}
.nav a:hover{background:var(--purple-50);color:var(--purple);text-decoration:none}
.nav a.cta{background:var(--navy);color:#fff;margin-left:8px;padding:9px 14px}
.nav a.cta:hover{background:#262650;color:#fff}
.chip{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:600;color:var(--ink-2);background:#fff;border:1px solid var(--line);border-radius:999px;padding:5px 11px;white-space:nowrap}
.chip:hover{border-color:var(--purple-100);text-decoration:none}
.dot{width:7px;height:7px;border-radius:50%;background:var(--purple);flex:none}
.dot.off{background:var(--orange)}
main{padding:40px 0 80px;animation:rise .4s cubic-bezier(.2,.7,.2,1) both}
body.live main{animation:none}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.site-foot{border-top:1px solid var(--line);background:#fff}
.site-foot .wrap{display:flex;flex-wrap:wrap;gap:8px 24px;justify-content:space-between;align-items:center;padding-top:22px;padding-bottom:22px;font-size:13.5px;color:var(--muted)}
.site-foot strong{color:var(--navy);font-weight:650}
@media(max-width:760px){.nav a:not(.cta){display:none}.topbar .chip{display:none}}

/* buttons and inputs */
.btn,button.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:600 14.5px/1.2 var(--font);padding:11px 18px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:#fff;color:var(--navy);cursor:pointer;text-decoration:none;transition:background .15s,border-color .15s,color .15s,box-shadow .15s,transform .1s}
.btn:hover{border-color:var(--purple);color:var(--purple);text-decoration:none}
.btn:active{transform:translateY(1px)}
.btn-primary,button.btn-primary{background:var(--purple);border-color:var(--purple);color:#fff;box-shadow:0 8px 20px -10px rgba(104,71,245,.8)}
.btn-primary:hover{background:var(--purple-700);border-color:var(--purple-700);color:#fff}
.btn-dark{background:var(--navy);border-color:var(--navy);color:#fff}.btn-dark:hover{background:#262650;color:#fff;border-color:#262650}
.btn-stop{color:var(--orange-700);border-color:var(--orange-100)}.btn-stop:hover{background:var(--orange-50);border-color:var(--orange);color:var(--orange-700)}
.btn-lg{padding:15px 26px;font-size:16px;border-radius:14px}
.btn .arrow{transition:transform .15s}.btn:hover .arrow{transform:translateX(3px)}
label{display:block;font-weight:650;font-size:13.5px;color:var(--navy);margin-bottom:6px}
label .opt{font-weight:450;color:var(--muted)}
input[type=text],textarea{width:100%;font:inherit;font-size:15px;color:var(--ink);background:#fff;border:1px solid var(--line-2);border-radius:var(--r-sm);padding:11px 14px;transition:border-color .15s,box-shadow .15s}
textarea{resize:vertical;line-height:1.55}
input[type=text]:hover,textarea:hover{border-color:#C5BFE0}
input[type=text]:focus,textarea:focus{outline:none;border-color:var(--purple);box-shadow:0 0 0 4px rgba(104,71,245,.14)}
::placeholder{color:#A6A4B8}
.field{margin-top:16px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
@media(max-width:640px){.grid2{grid-template-columns:1fr}}

/* surfaces */
.card{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:24px;box-shadow:var(--shadow)}
.stack>*+*{margin-top:16px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:750;letter-spacing:.05em;text-transform:uppercase;padding:4px 10px;border-radius:999px;line-height:1.35;white-space:nowrap;vertical-align:middle}
.pill.purple{background:var(--purple-50);color:var(--purple-700)}
.pill.orange{background:var(--orange-50);color:var(--orange-700)}
.pill.navy{background:var(--navy);color:#fff}
.pill.muted{background:var(--navy-50);color:var(--muted)}
.pill.solid{background:var(--purple);color:#fff}
.pill.solid-orange{background:var(--orange);color:#fff}
.kicker{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--purple);margin:0 0 8px}
.label{font-size:12px;font-weight:750;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
.icon{flex:none;width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-weight:800;font-size:14px;line-height:1}
.icon.purple{background:var(--purple);color:#fff}.icon.orange{background:var(--orange);color:#fff}.icon.navy{background:var(--navy);color:#fff}
.alert{display:flex;gap:14px;align-items:flex-start;padding:16px 18px;border-radius:var(--r);border:1px solid var(--orange-100);background:var(--orange-50);color:var(--ink)}
.alert.info{border-color:var(--purple-100);background:var(--purple-50)}
.alert h2,.alert h3{font-size:16px;margin:2px 0 6px}
.alert details{margin-top:8px}.alert summary{cursor:pointer;font-size:13px;font-weight:650;color:var(--ink-2)}
.alert pre{white-space:pre-wrap;word-break:break-word;font:12.5px/1.5 var(--mono);background:rgba(255,255,255,.7);border-radius:8px;padding:10px;margin:8px 0 0}
.notice{display:flex;gap:12px;align-items:flex-start;padding:16px 18px;border:1px dashed var(--line-2);border-radius:var(--r);background:#fff;font-size:14px;color:var(--ink-2)}
details.fold{background:#fff;border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow-sm)}
details.fold>summary{list-style:none;cursor:pointer;padding:18px 22px;font-weight:700;color:var(--navy);display:flex;align-items:center;gap:12px}
details.fold>summary::-webkit-details-marker,details.more>summary::-webkit-details-marker{display:none}
details.fold>summary::after{content:"";margin-left:auto;width:8px;height:8px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(45deg);transition:transform .2s}
details.fold[open]>summary::after{transform:rotate(-135deg)}
details.fold>summary:hover{color:var(--purple)}
.fold-body{padding:0 22px 22px}
details.more{margin-top:10px}
details.more>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:700;color:var(--purple)}
details.more>summary::after{content:"";width:6px;height:6px;border-right:2px solid currentColor;border-bottom:2px solid currentColor;transform:rotate(45deg);margin-top:-3px;transition:transform .2s}
details.more[open]>summary::after{transform:rotate(-135deg);margin-top:3px}
details.more>div{margin-top:8px;font-size:14px;color:var(--ink-2)}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r);background:#fff;box-shadow:var(--shadow-sm)}
table{border-collapse:collapse;width:100%;font-size:14px}
th{font-size:11.5px;font-weight:750;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);background:#FBFAFE;text-align:left;padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:bottom}
td{padding:14px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}

/* landing */
.hero{text-align:center;padding:36px 0 36px}
.eyebrow{display:inline-flex;gap:8px;align-items:center;font-size:12.5px;font-weight:650;letter-spacing:.04em;color:var(--purple-700);background:#fff;border:1px solid var(--purple-100);padding:6px 14px;border-radius:999px;box-shadow:var(--shadow-sm)}
.hero h1{font-size:clamp(36px,5.6vw,60px);line-height:1.04;letter-spacing:-.04em;margin:22px auto 18px;max-width:860px;font-weight:850}
.hero h1 .hl{color:var(--purple);background:linear-gradient(transparent 72%,rgba(255,113,56,.30) 72%,rgba(255,113,56,.30) 92%,transparent 92%)}
.hero .lead{font-size:18px;color:var(--muted);max-width:660px;margin:0 auto;line-height:1.55}
.composer{max-width:820px;margin:0 auto;background:#fff;border:1px solid var(--line);border-radius:24px;box-shadow:var(--shadow-lg);padding:30px}
.composer h2{font-size:20px}
.composer .sub{color:var(--muted);font-size:14.5px;margin:6px 0 0}
.idea-box{margin-top:18px;border:1.5px solid var(--line-2);border-radius:16px;padding:16px 18px;background:#fff;transition:border-color .15s,box-shadow .15s}
.idea-box:focus-within{border-color:var(--purple);box-shadow:0 0 0 5px rgba(104,71,245,.12)}
.idea-box label{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--purple-700)}
.idea-box textarea{border:none;padding:0;font-size:18px;line-height:1.5;min-height:96px;box-shadow:none;background:transparent}
.idea-box textarea:focus{box-shadow:none}
.context{margin-top:20px;padding-top:4px}
.context .label{margin-top:4px}
details.sources-add{margin-top:16px}
details.sources-add>summary{cursor:pointer;font-size:14px;font-weight:650;color:var(--purple)}
.form-foot{display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between;margin-top:24px;padding-top:20px;border-top:1px solid var(--line)}
.form-foot .meta{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted)}
.form-foot .meta span::before{content:"";display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--orange);margin-right:7px;vertical-align:middle}
.how{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:56px}
.how .step{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:20px}
.how .n{width:30px;height:30px;border-radius:9px;background:var(--purple-50);color:var(--purple-700);display:grid;place-items:center;font-weight:800;font-size:13.5px;margin-bottom:12px}
.how .step:last-child .n{background:var(--orange);color:#fff}
.how h3{font-size:15.5px;margin-bottom:6px}.how p{font-size:14px;color:var(--muted)}
@media(max-width:900px){.how{grid-template-columns:1fr 1fr}}@media(max-width:560px){.how{grid-template-columns:1fr}}
.section-head{display:flex;align-items:end;justify-content:space-between;gap:12px;margin:56px 0 16px}
.proj-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.proj{display:flex;flex-direction:column;gap:12px;padding:18px 20px;background:#fff;border:1px solid var(--line);border-radius:var(--r);color:var(--navy);box-shadow:var(--shadow-sm);transition:border-color .15s,box-shadow .15s,transform .15s}
.proj:hover{text-decoration:none;border-color:var(--purple-100);box-shadow:var(--shadow);transform:translateY(-2px)}
.proj strong{font-size:15.5px;line-height:1.35}
.proj .id{font-size:12px;color:var(--muted);font-family:var(--mono)}
.proj .pill,.proj-head .pill{white-space:normal;line-height:1.4;max-width:100%}

/* project page */
.proj-head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px}
.back{display:inline-flex;align-items:center;gap:6px;font-size:13.5px;font-weight:600;color:var(--muted);margin-bottom:14px}
.back:hover{color:var(--purple);text-decoration:none}
.track{display:grid;grid-template-columns:repeat(8,1fr);gap:8px;list-style:none;margin:26px 0 22px;padding:0}
.track li{position:relative;padding-top:16px;font-size:12px;color:var(--muted);line-height:1.35}
.track li::before{content:"";position:absolute;top:0;left:0;right:0;height:5px;border-radius:99px;background:var(--line)}
.track li.done::before{background:var(--purple)}
.track li.done{color:var(--ink-2)}
.track li.now::before{background:var(--orange)}
.track li.now{color:var(--navy);font-weight:750}
.track li.stub::before{background:repeating-linear-gradient(90deg,var(--line-2) 0 6px,transparent 6px 11px)}
.track li .tag{display:block;font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#A6A4B8;margin-top:2px}
@media(max-width:760px){.track{grid-template-columns:repeat(4,1fr);row-gap:16px}}
.brief-table{width:100%}.brief-table th{width:130px;background:none;border:none;padding:8px 0;font-size:11.5px;vertical-align:top}
.brief-table td{border:none;padding:8px 0;color:var(--ink-2)}
.decision{position:relative;overflow:hidden;background:#fff;border:1px solid var(--purple-100);border-radius:var(--r-lg);box-shadow:var(--shadow-lg);padding:28px}
.decision::before{content:"";position:absolute;inset:0 0 auto 0;height:4px;background:var(--purple)}
.decision.warn{border-color:var(--orange-100)}.decision.warn::before{background:var(--orange)}
.decision h2{font-size:22px}
.decision .hint{color:var(--muted);font-size:14px;margin:14px 0 0}
.rec{display:flex;gap:14px;align-items:flex-start;margin-top:18px;padding:16px 18px;border-radius:14px;background:var(--bg);border:1px solid var(--line)}
.rec.continue{background:var(--purple-50);border-color:var(--purple-100)}
.rec.repeat{background:var(--orange-50);border-color:var(--orange-100)}
.rec.stop{background:var(--navy-50);border-color:#DCDCE8}
.rec p{margin:6px 0 0;color:var(--ink-2);font-size:14.5px}
.choices{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}
@media(max-width:820px){.choices{grid-template-columns:1fr 1fr}}
.choice{position:relative;display:flex;flex-direction:column;align-items:flex-start;gap:4px;width:100%;height:100%;text-align:left;padding:15px 16px;border-radius:14px;border:1px solid var(--line-2);background:#fff;cursor:pointer;font:inherit;color:var(--navy);transition:border-color .15s,box-shadow .15s,transform .1s,background .15s}
.choice strong{font-size:15px}.choice small{color:var(--muted);font-size:12.5px;line-height:1.4}
.choice:hover{border-color:var(--purple);box-shadow:0 0 0 4px rgba(104,71,245,.10)}
.choice:active{transform:translateY(1px)}
.choice.primary{background:var(--purple);border-color:var(--purple);color:#fff}
.choice.primary strong{color:#fff}.choice.primary small{color:rgba(255,255,255,.82)}
.choice.primary:hover{background:var(--purple-700)}
.choice.stop strong{color:var(--orange-700)}
.choice.stop:hover{border-color:var(--orange);box-shadow:0 0 0 4px rgba(255,113,56,.12)}
.choice.is-rec{box-shadow:0 0 0 3px var(--orange)}
.choice .rec-tag{position:absolute;top:-10px;right:10px;font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;background:var(--orange);color:#fff;padding:3px 8px;border-radius:999px}
details.typed{margin-top:14px}details.typed>summary{cursor:pointer;font-size:13px;color:var(--muted);font-weight:600}
.typed-row{display:flex;gap:8px;margin-top:10px;max-width:460px}
.working{position:relative;overflow:hidden}
.progress{height:4px;background:var(--purple-50);border-radius:99px;overflow:hidden;margin:18px 0 6px}
.progress span{display:block;width:38%;height:100%;border-radius:99px;background:var(--purple);animation:slide 1.6s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(270%)}}
.live-steps{list-style:none;padding:0;margin:18px 0 0;display:grid;gap:12px}
.live-steps li{display:flex;gap:12px;align-items:center;color:#A6A4B8;font-size:14.5px}
.live-steps .st{width:22px;height:22px;border-radius:50%;border:2px solid var(--line-2);flex:none;display:grid;place-items:center;font-size:11px;font-weight:800;color:#fff}
.live-steps li.done{color:var(--ink-2)}.live-steps li.done .st{background:var(--purple);border-color:var(--purple)}
.live-steps li.now{color:var(--navy);font-weight:650}
.live-steps li.now .st{border-color:var(--purple-100);border-top-color:var(--purple);animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.page-foot{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);font-size:13px;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px 18px}

/* results */
.results{margin-top:40px}
.doc-head .meta{color:var(--muted);font-size:14px;margin-top:8px}
.tiles{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:12px;margin-top:20px}
@media(max-width:820px){.tiles{grid-template-columns:1fr}}
.tile{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;box-shadow:var(--shadow-sm)}
.tile .v{font-size:24px;font-weight:850;letter-spacing:-.02em;color:var(--navy);margin:2px 0 4px;line-height:1.2}
.tile .v.purple{color:var(--purple)}.tile .v.orange{color:var(--orange-700)}
.tile p{font-size:13.5px;color:var(--ink-2);margin:0}
.block{margin-top:44px}
.block-head{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;margin-bottom:16px}
.kind{font-size:10.5px;font-weight:800;letter-spacing:.09em;text-transform:uppercase;padding:4px 9px;border-radius:7px}
.kind.ai{background:var(--navy-50);color:var(--ink-2)}
.kind.ev{background:var(--purple-50);color:var(--purple-700)}
.score-table td.opt{min-width:230px}.score-table td.cust{min-width:200px;color:var(--ink-2);font-size:13.5px}
.optlabel{display:block;font-size:11px;font-weight:750;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
tr.picked td{background:#FCFBFF}
tr.picked td.opt{box-shadow:inset 3px 0 0 var(--purple)}
.meter{display:inline-flex;gap:3px;margin-right:8px;vertical-align:middle}
.meter i{width:7px;height:14px;border-radius:2px;background:var(--line)}
.meter i.on{background:var(--purple)}
.score{white-space:nowrap;font-weight:750}
.total{white-space:nowrap;font-weight:850;font-size:16px;color:var(--navy)}.total small{color:var(--muted);font-weight:550;font-size:12px}
.bar{height:6px;background:var(--line);border-radius:99px;margin-top:6px;width:84px;overflow:hidden}
.bar span{display:block;height:100%;background:var(--purple);border-radius:99px}
.opt-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:16px}
@media(max-width:920px){.opt-grid{grid-template-columns:1fr}}
.opt-card{display:flex;flex-direction:column;background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:20px;box-shadow:var(--shadow-sm)}
.opt-card.picked{border-color:var(--purple-100);box-shadow:0 0 0 1px var(--purple-100),var(--shadow)}
.opt-card .top{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}
.opt-card h3{font-size:15.5px;margin-bottom:12px}
.opt-card dl{margin:0;display:grid;gap:10px}
.opt-card dt{font-size:11.5px;font-weight:750;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.opt-card dd{margin:2px 0 0;font-size:14px;color:var(--ink-2)}
.callout{display:flex;gap:14px;align-items:flex-start;margin-top:16px;padding:18px 20px;border-radius:var(--r);border:1px solid var(--purple-100);background:var(--purple-50)}
.callout.orange{border-color:var(--orange-100);background:var(--orange-50)}
.callout p{color:var(--ink-2)}
.note{font-size:13.5px;color:var(--muted);margin-top:12px}
ul.points{list-style:none;padding:0;margin:0;display:grid;gap:10px}
ul.points li{position:relative;padding-left:20px;color:var(--ink-2)}
ul.points li::before{content:"";position:absolute;left:2px;top:.62em;width:7px;height:7px;border-radius:2px;background:var(--purple)}
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:13px;color:var(--muted);margin:12px 0 16px;align-items:center}
.legend span{display:inline-flex;gap:8px;align-items:center}
.claims{display:grid;gap:12px}
.claim{display:grid;grid-template-columns:auto 1fr;gap:14px;padding:18px 20px;border:1px solid var(--line);border-radius:var(--r);background:#fff;box-shadow:var(--shadow-sm)}
.claim.assumption{border-style:dashed;border-color:#F0C2AB;background:#FFFCFA;box-shadow:none}
.cstat{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-weight:850;font-size:13px;color:#fff;background:var(--purple)}
.claim.assumption .cstat{background:#fff;color:var(--orange-700);border:2px dashed var(--orange)}
.claim-text{font-weight:600;color:var(--navy);margin:2px 0 8px}
.claim .why{font-size:13px;color:var(--orange-700);margin-left:6px}
blockquote.quote{margin:12px 0 0;padding:10px 14px;border-left:3px solid var(--purple);background:var(--bg);border-radius:0 10px 10px 0;color:var(--ink-2);font-size:14px}
.cite{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin-top:10px;font-size:13px}
.cite a.src{font-weight:650}
.host{color:var(--muted)}
a.num,.num{flex:none;min-width:24px;height:24px;padding:0 6px;border-radius:7px;background:var(--purple-50);color:var(--purple-700);font-weight:800;font-size:12px;display:inline-grid;place-items:center}
a.num:hover{text-decoration:none;background:var(--purple-100)}
.sources-panel{margin-top:20px;padding:20px;border-radius:var(--r);background:#fff;border:1px solid var(--line)}
ol.sources{list-style:none;padding:0;margin:0;display:grid;gap:10px}
ol.sources li{display:flex;gap:12px;align-items:flex-start;font-size:14px}
ol.sources li:target{background:var(--purple-50);border-radius:10px;box-shadow:0 0 0 6px var(--purple-50)}
ol.sources .meta{display:block;font-size:12.5px;color:var(--muted)}
.crit-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.crit-grid .full{grid-column:1/-1}
@media(max-width:820px){.crit-grid{grid-template-columns:1fr}}
.panel{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:20px 22px;box-shadow:var(--shadow-sm)}
.panel.strong{border-color:var(--purple-100);background:linear-gradient(#fff,#fff) padding-box}
.panel.strong p{font-size:15.5px;color:var(--navy)}
ol.icons{list-style:none;padding:0;margin:0;display:grid;gap:12px;counter-reset:n}
ol.icons li{display:grid;grid-template-columns:26px 1fr;gap:12px;color:var(--ink-2);font-size:14.5px;counter-increment:n}
ol.icons li::before{content:counter(n);width:26px;height:26px;border-radius:8px;display:grid;place-items:center;font-size:12px;font-weight:850;background:var(--orange-50);color:var(--orange-700)}
ul.kill{list-style:none;padding:0;margin:0;display:grid;gap:12px}
ul.kill li{display:grid;grid-template-columns:26px 1fr;gap:12px;color:var(--ink-2);font-size:14.5px}
ul.kill li::before{content:"\\00D7";width:26px;height:26px;border-radius:8px;display:grid;place-items:center;font-size:16px;font-weight:800;background:var(--navy);color:#fff}
.tweaks{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.tweak{padding:16px 18px;border-radius:14px;border:1px solid var(--line);background:var(--bg)}
.tweak strong{display:block;color:var(--navy);font-size:14.5px;margin-bottom:6px}
.tweak p{font-size:13.5px;color:var(--ink-2)}
.tweak .label{margin:0 0 2px;font-size:10.5px}

/* generic documents (markdown written by the workflow) */
.doc .doc-intro{margin-bottom:6px}
.doc .doc-intro h1{font-size:24px;margin-bottom:10px}
.doc .doc-intro p{color:var(--ink-2)}
.doc-sec{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:22px 24px;margin-top:14px;box-shadow:var(--shadow-sm)}
.doc-sec h2{font-size:17px;margin:0 0 12px}
.doc-sec h3{font-size:15px;margin:14px 0 8px}
.doc-sec p,.doc-sec li{color:var(--ink-2)}
.doc-sec ul,.doc-sec ol{padding-left:20px;margin:0}
.doc-sec li+li{margin-top:6px}
.doc .table-wrap{margin:8px 0}
.doc-sec[data-h^="questions"] ol{list-style:none;padding:0;counter-reset:q;display:grid;gap:8px}
.doc-sec[data-h^="questions"] li{counter-increment:q;position:relative;margin:0;padding:12px 14px 12px 52px;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--navy)}
.doc-sec[data-h^="questions"] li::before{content:counter(q);position:absolute;left:14px;top:11px;width:26px;height:26px;border-radius:8px;background:var(--purple);color:#fff;font-weight:800;font-size:12.5px;display:grid;place-items:center}
.doc-sec[data-h^="signs-of"] ul,.doc-sec[data-h^="do-not-ask"] ul{list-style:none;padding:0;display:grid;gap:8px}
.doc-sec[data-h^="signs-of"] li,.doc-sec[data-h^="do-not-ask"] li{position:relative;margin:0;padding-left:32px}
.doc-sec[data-h^="signs-of"] li::before{content:"\\2713";position:absolute;left:0;top:1px;width:22px;height:22px;border-radius:7px;background:var(--purple-50);color:var(--purple-700);font-weight:800;font-size:12px;display:grid;place-items:center}
.doc-sec[data-h^="do-not-ask"] li::before{content:"\\00D7";position:absolute;left:0;top:1px;width:22px;height:22px;border-radius:7px;background:var(--orange-50);color:var(--orange-700);font-weight:800;font-size:15px;display:grid;place-items:center}
.doc-sec[data-h^="opening"] p{font-size:16px;color:var(--navy);border-left:3px solid var(--orange);padding-left:14px}

/* launch drafts */
.draft-card .head{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
pre.draft{white-space:pre-wrap;word-break:break-word;font:inherit;font-size:14.5px;line-height:1.6;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:14px 0 10px;color:var(--ink)}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.fingerprint{font-size:12.5px;color:var(--muted)}

@media(prefers-reduced-motion:reduce){main{animation:none}*{transition:none!important;animation-duration:.01ms!important}}
@media(max-width:560px){.wrap{padding:0 16px}main{padding-top:24px}.composer,.decision,.card{padding:20px}}
"""


# ---------------------------------------------------------------------- layout


def page(title: str, body: str, *, refresh: int | None = None, model: str = "", model_ready: bool | None = None) -> str:
    """Full HTML page with the shared header, footer and styles."""
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    chip = ""
    if model:
        dot = "dot" if model_ready is not False else "dot off"
        chip = f'<a class="chip" href="/status" title="Active local model"><span class="{dot}"></span>{esc(model)}</a>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">{meta}'
        f"<title>{esc(title)} · Foundry</title><style>{CSS}</style></head>"
        f'<body class="{"live" if refresh else ""}">'
        '<header class="topbar"><div class="wrap">'
        '<a class="brand" href="/">'
        '<img class="brand-logo" src="/static/brand/logo.png" alt="Foundry" width="173" height="40"></a>'
        f'<nav class="nav">{chip}<a href="/#projects">Projects</a><a href="/status">System status</a>'
        '<a class="cta" href="/#new">New idea</a></nav>'
        "</div></header>"
        f'<main><div class="wrap">{body}</div></main>'
        '<footer class="site-foot"><div class="wrap">'
        f"<strong>{esc(SLOGAN)}</strong>"
        "<span>Runs on your machine with an open model. No account, no payments.</span>"
        "</div></footer></body></html>"
    )


def pill(text: str, tone: str = "purple") -> str:
    return f'<span class="pill {tone}">{esc(text)}</span>'


def alert(title: str, body_html: str, *, tone: str = "", icon: str = "!") -> str:
    icon_tone = "purple" if tone == "info" else "orange"
    return (f'<div class="alert {tone}"><span class="icon {icon_tone}">{esc(icon)}</span>'
            f"<div><h3>{esc(title)}</h3>{body_html}</div></div>")


# ---------------------------------------------------------------------- forms

CONTEXT_FIELDS = (
    ("target_market", "Who is it for", "For example: freelance designers in Egypt"),
    ("skills", "Your skills", "For example: Python, design, teaching"),
    ("budget", "Budget", "For example: $100"),
    ("time_available", "Time you have", "For example: 10 hours a week for 2 months"),
)


def brief_form(brief: dict[str, Any] | None = None, action: str = "/projects", submit: str = "Challenge my idea") -> str:
    b = brief or {}

    def text_field(name: str, label: str, hint: str) -> str:
        return (f'<div class="field"><label for="f-{name}">{esc(label)} <span class="opt">(optional)</span></label>'
                f'<input id="f-{name}" type="text" name="{name}" value="{esc(b.get(name, ""))}" placeholder="{esc(hint)}"></div>')

    urls = esc("\n".join(b.get("source_urls", [])))
    return (
        f'<form method="post" action="{esc(action)}">'
        '<div class="idea-box"><label for="f-idea">Your idea</label>'
        '<textarea id="f-idea" name="idea" rows="3" required minlength="10" '
        'placeholder="What digital product do you want to build, and for whom? One or two sentences is enough.">'
        f'{esc(b.get("idea", ""))}</textarea></div>'
        '<div class="context"><p class="label">Context for a sharper critique</p>'
        '<div class="grid2">' + "".join(text_field(*f) for f in CONTEXT_FIELDS) + "</div>"
        + text_field("values", "Your values", "What you will and will not do, for example: honest marketing")
        + '<details class="sources-add"><summary>Add pages you want the agents to read</summary>'
        '<div class="field"><label for="f-urls">Source URLs <span class="opt">(one per line, up to 5)</span></label>'
        f'<textarea id="f-urls" name="source_urls" rows="2" placeholder="https://...">{urls}</textarea></div></details></div>'
        '<div class="form-foot"><div class="meta"><span>About 2 minutes</span><span>Runs locally</span>'
        "<span>You decide at every step</span></div>"
        f'<button class="btn btn-primary btn-lg" type="submit">{esc(submit)} <span class="arrow">&rarr;</span></button>'
        "</div></form>"
    )


# ---------------------------------------------------------------------- gates and progress

CHOICE_TEXT = {
    "continue": ("Continue", "Accept this and move to the next step"),
    "repeat": ("Repeat", "Run this step again for a better result"),
    "go_back": ("Go back", "Return to the previous step or change the brief"),
    "stop": ("Stop", "End this project here"),
}


def choice_buttons(project_id: str, choices: Iterable[str], allowed: tuple[str, ...], recommended: str = "",
                   action: str = "decide", typed: bool = True) -> str:
    buttons = ""
    for choice in choices:
        if choice not in allowed:
            continue
        label, hint = CHOICE_TEXT[choice]
        css = {"continue": "primary", "stop": "stop"}.get(choice, "")
        rec = choice == recommended
        tag = '<span class="rec-tag">Recommended</span>' if rec else ""
        sr = '<span class="sr"> (recommended)</span>' if rec else ""
        buttons += (f'<button class="choice {css}{" is-rec" if rec else ""}" name="choice" value="{choice}" type="submit">'
                    f"{tag}<strong>{label}{sr}</strong><small>{hint}</small></button>")
    out = (f'<form method="post" action="/projects/{esc(project_id)}/{action}">'
           f'<div class="choices">{buttons}</div></form>')
    if typed:
        out += (f'<details class="typed"><summary>Or type your choice</summary>'
                f'<form class="typed-row" method="post" action="/projects/{esc(project_id)}/decide">'
                '<input type="text" name="reply" placeholder="continue, repeat, go back or stop">'
                '<button class="btn" type="submit">Send</button></form></details>')
    return out


def recommendation(state: dict[str, Any]) -> str:
    rec = str(state.get("recommendation", ""))
    tone = {"continue": ("continue", "solid", "&#10003;", "purple"), "repeat": ("repeat", "solid-orange", "&#8635;", "orange"),
            "stop": ("stop", "navy", "&#9632;", "navy")}.get(rec, ("", "muted", "?", "navy"))
    return (f'<div class="rec {tone[0]}"><span class="icon {tone[3]}">{tone[2]}</span><div>'
            f'<span class="pill {tone[1]}">Foundry recommends: {esc(rec.upper())}</span>'
            f'<p>{esc(state.get("recommendation_reason", ""))}</p></div></div>')


def decision_panel(title: str, state: dict[str, Any], hint: str, buttons: str, *, kicker: str = "Your decision") -> str:
    return (f'<section class="decision"><p class="kicker">{esc(kicker)}</p><h2>{esc(title)}</h2>'
            f'{recommendation(state)}<p class="hint">{esc(hint)}</p>{buttons}</section>')


def stepper(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<li class="{css}">{esc(name)}{"<span class=tag>STUBBED</span>" if css == "stub" else ""}</li>'
        for name, css in items
    )
    return f'<ol class="track" aria-label="Progress through the six phases">{cells}</ol>'


STEP1_STAGES = (
    ("screening the brief", "Checking what kind of product this is"),
    ("safety check", "Running the safety check on your brief"),
    ("searching and reading sources", "Searching the web and reading sources"),
    ("finding opportunities", "Sharpening the idea and scoring alternatives"),
    ("critic review", "Critic review: risks, kill criteria, cheapest test"),
)
STEP2_STAGES = (("drafting the interview script", "Drafting the Mom Test interview script"),)
LAUNCH_STAGES = (("drafting", "Drafting the store page and launch posts, then checking each draft with the guardrails"),)
COMMERCIAL_STAGES = (("drafting", "Searching what similar products charge, drafting price options and the terms, privacy and refund policies, then checking each policy with the guardrails"),)
VALIDATE_STAGES = (("drafting", "Drafting the Lean Canvas, demand-test plan, landing page and invite post, then checking the drafts with the guardrails"),)


def working_card(stage: str, kind: str) -> str:
    stages = {"step1": STEP1_STAGES, "step2": STEP2_STAGES, "launch": LAUNCH_STAGES,
              "validate": VALIDATE_STAGES, "commercial": COMMERCIAL_STAGES}.get(kind, ())
    keys = [key for key, _ in stages]
    current = keys.index(stage) if stage in keys else 0
    items = ""
    for index, (_, label) in enumerate(stages):
        css = "done" if index < current else ("now" if index == current else "")
        mark = "&#10003;" if css == "done" else ""
        items += f'<li class="{css}"><span class="st">{mark}</span>{esc(label)}</li>'
    title = {"step1": "Researching and challenging your idea", "step2": "Writing your interview script",
             "launch": "Drafting your launch material",
             "validate": "Designing the business and the demand test",
             "commercial": "Setting the price and drafting your policies"}.get(kind, "The agents are working")
    return (
        f'<section class="card working"><p class="kicker">The agents are working: {esc(stage or "starting")}</p>'
        f"<h2>{esc(title)}</h2>"
        '<div class="progress" aria-hidden="true"><span></span></div>'
        f'<ol class="live-steps">{items}</ol>'
        '<p class="muted small" style="margin-top:18px">This usually takes one to two minutes. '
        "The page updates by itself; you can leave it open.</p></section>"
    )


def friendly_error(raw: str) -> tuple[str, str]:
    """A plain explanation of a failed step. The raw error is always shown as well."""
    text = raw.lower()
    if "ollama request failed" in text or "connection refused" in text and "11434" in text:
        return ("Foundry could not reach the local model.",
                "Check that Ollama is running and the model is downloaded (see System status), then run the step again.")
    if "timed out" in text or "timeout" in text:
        return ("The local model took too long to answer.",
                "Large models can be slow on a laptop. Run the step again; if it keeps happening, raise FOUNDRY_LLM_TIMEOUT.")
    if "structured output validation failed" in text:
        return ("The model's answer did not match the required format, even after automatic repairs.",
                "Running the step again usually works.")
    return ("The step stopped with an error.", "You can run the step again.")


# ---------------------------------------------------------------------- documents


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", text).lower()).strip("-")


def doc_html(text: str) -> str:
    """Render a markdown document written by the workflow as section cards.

    Model output is escaped first, so it can never inject HTML. Headings are untouched."""
    body = markdown_lib.markdown(html.escape(text, quote=False), extensions=["tables"])
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    for label, tone in (("ASSUMPTION", "orange"), ("MOCKED", "orange"), ("STUBBED", "orange")):
        body = body.replace(f"<code>{label}</code>", f'<span class="pill {tone}">{label}</span>')
    parts = re.split(r"(<h2>.*?</h2>)", body, flags=re.S)
    out = [f'<header class="doc-intro">{parts[0]}</header>'] if parts[0].strip() else []
    for index in range(1, len(parts), 2):
        content = parts[index + 1] if index + 1 < len(parts) else ""
        out.append(f'<section class="doc-sec" data-h="{_slug(parts[index])}">{parts[index]}{content}</section>')
    return f'<div class="doc">{"".join(out)}</div>'


# ---------------------------------------------------------------------- Discover results


def _host(url: str) -> str:
    host = urlparse(url).hostname or url
    return host[4:] if host.startswith("www.") else host


def _date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%-d %b %Y")
    except ValueError:
        return iso[:10]


_ABBREV = re.compile(r"(?:\b(?:[A-Za-z]\.){1,3}|\b(?:e\.g|i\.e|etc|vs|approx|Dr|Mr|Ms|St|No)\.)$")


def _sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text.strip())
    merged: list[str] = []
    for piece in pieces:
        if merged and _ABBREV.search(merged[-1]):
            merged[-1] += " " + piece
        else:
            merged.append(piece)
    return [s for s in merged if s]


def _paragraph_or_points(text: str) -> str:
    sentences = _sentences(text)
    if len(sentences) >= 3:
        return '<ul class="points">' + "".join(f"<li>{esc(s)}</li>" for s in sentences) + "</ul>"
    return f"<p>{esc(text)}</p>"


def _meter(value: int) -> str:
    cells = "".join(f'<i class="{"on" if i < value else ""}"></i>' for i in range(5))
    return f'<span class="score"><span class="meter" aria-hidden="true">{cells}</span>{value}</span>'


_NOTE_SPLIT = re.compile(
    r"\s+(?=(?:Web search failed|Skipped a page|A search query|A page fetch|No page could))"
)


def opportunities_view(
    *,
    project_id: str,
    research: dict[str, Any],
    critic: dict[str, Any],
    picked_title: str,
    claims: list[Claim],
    cited_pages: dict[str, FetchedPage],
    all_pages: list[FetchedPage],
    recommendation: str,
    reason: str,
    search_note: str,
) -> str:
    """Phase 1, step 1 as cards. Same data, headings and order as `phase1_step1_opportunities.md`."""
    options = [research["sharpened_idea"], *research["alternatives"]]
    labels = ["Sharpened idea"] + [f"Alternative {i}" for i in range(1, len(options))]
    facts = [c for c in claims if c.claim_type == ClaimType.FACT]
    totals = [o["problem_severity"] + o["reachable_market"] + o["founder_fit"] + o["cheap_first_test"] for o in options]
    rec_tone = {"continue": "purple", "repeat": "orange", "stop": ""}.get(recommendation, "")

    head = (
        '<header class="doc-head"><p class="kicker">Results</p>'
        "<h1>Phase 1, step 1: opportunities</h1>"
        f'<p class="meta">Project <code>{esc(project_id)}</code>. Generated by the researcher and critic agents.</p>'
        "</header>"
        '<div class="tiles">'
        f'<div class="tile"><p class="label">System recommendation</p><div class="v {rec_tone}">{esc(recommendation.upper())}.</div>'
        f"<p>{esc(reason)}</p></div>"
        f'<div class="tile"><p class="label">Critic</p><div class="v">{esc(critic["recommendation"])}</div>'
        f'<p>confidence <strong>{esc(critic["confidence"])}</strong></p></div>'
        f'<div class="tile"><p class="label">Evidence</p><div class="v {"purple" if facts else "orange"}">'
        f"{len(facts)} sourced claims</div><p>{len(claims) - len(facts)} labelled {pill('ASSUMPTION', 'orange')}</p></div>"
        "</div>"
    )

    # Options scored on the same criteria
    rows = ""
    for label, option, total in zip(labels, options, totals):
        picked = option["title"] == picked_title
        tag = f" {pill('Recommended', 'solid')}" if picked else ""
        rows += (
            f'<tr class="{"picked" if picked else ""}"><td class="opt"><span class="optlabel">{esc(label)}</span>'
            f'<strong>{esc(option["title"])}</strong>{tag}</td><td class="cust">{esc(option["customer"])}</td>'
            + "".join(f"<td>{_meter(int(option[k]))}</td>" for k in
                      ("problem_severity", "reachable_market", "founder_fit", "cheap_first_test"))
            + f'<td><span class="total">{total}<small> /20</small></span>'
            f'<div class="bar"><span style="width:{total * 5}%"></span></div></td></tr>'
        )
    table = (
        '<div class="table-wrap"><table class="score-table"><tr><th>Option</th><th>Customer</th>'
        "<th>Problem severity</th><th>Reachable market</th><th>Founder fit</th><th>Cheap first test</th>"
        f"<th>Total /20</th></tr>{rows}</table></div>"
    )
    cards = ""
    for label, option, total in zip(labels, options, totals):
        picked = option["title"] == picked_title
        cards += (
            f'<article class="opt-card{" picked" if picked else ""}"><div class="top">'
            f'<span class="optlabel">{esc(label)}</span><span class="pill {"solid" if picked else "muted"}">{total} /20</span></div>'
            f'<h3>{esc(label)}: {esc(option["title"])}</h3><dl>'
            f'<div><dt>Problem:</dt><dd>{esc(option["problem"])}</dd></div>'
            f'<div><dt>Product:</dt><dd>{esc(option["product"])}</dd></div></dl>'
            f'<details class="more"><summary>Why these scores:</summary><div>{esc(option["score_reason"])}</div></details>'
            "</article>"
        )
    options_block = (
        '<section class="block"><div class="block-head"><h2>Options scored on the same criteria</h2>'
        '<span class="kind ai">Agent judgement</span></div>'
        f'{table}<div class="opt-grid">{cards}</div>'
        '<div class="callout"><span class="icon purple">&#9733;</span><div>'
        f'<p><strong>Researcher would test first:</strong> {esc(research["recommended"])}. {esc(research["recommended_reason"])}</p>'
        "</div></div>"
        '<p class="note">The alternatives are options for the founder. The idea is not changed unless the founder chooses to.</p>'
        "</section>"
    )

    market_block = (
        '<section class="block"><div class="block-head"><h2>Market</h2><span class="kind ai">Agent judgement</span></div>'
        f'<div class="panel">{_paragraph_or_points(research["market_definition"])}</div></section>'
    )

    # Claims and evidence: citations sit next to the claim they support.
    numbers: dict[str, int] = {}
    for claim in facts:
        if claim.source_url and claim.source_url not in numbers:
            numbers[claim.source_url] = len(numbers) + 1
    claim_cards = ""
    for claim in claims:
        if claim.claim_type == ClaimType.FACT and claim.source_url:
            page_ = cited_pages.get(claim.source_url)
            title = (page_.title if page_ and page_.title else "") or _host(claim.source_url)
            date = _date(claim.retrieved_date or (page_.fetched_at if page_ else None))
            n = numbers[claim.source_url]
            claim_cards += (
                '<article class="claim fact"><span class="cstat" title="Sourced">&#10003;</span><div>'
                f'<p class="claim-text">{esc(claim.text)}</p>{pill("Sourced", "purple")}'
                f'<blockquote class="quote">&ldquo;{esc(claim.quote or "")}&rdquo;</blockquote>'
                f'<p class="cite"><a class="num" href="#src-{n}" aria-label="Source {n}">{n}</a>'
                f'<a class="src" href="{esc(claim.source_url)}" target="_blank" rel="noopener noreferrer nofollow">{esc(title)}</a>'
                f'<span class="host">{esc(_host(claim.source_url))}</span>'
                f'{f"<span class=host>&middot; Retrieved {esc(date)}</span>" if date else ""}</p>'
                "</div></article>"
            )
        else:
            claim_cards += (
                '<article class="claim assumption"><span class="cstat" title="Assumption">?</span><div>'
                f'<p class="claim-text">{esc(claim.text)}</p>{pill("ASSUMPTION", "orange")}'
                '<span class="why">Not supported by a fetched source.</span></div></article>'
            )

    def source_item(n: int | None, page_: FetchedPage) -> str:
        title = page_.title or _host(page_.url)
        badge = f'<span class="num">{n}</span>' if n else '<span class="num">&middot;</span>'
        return (f"<li{f' id=src-{n}' if n else ''}>{badge}<div>"
                f'<a href="{esc(page_.url)}" target="_blank" rel="noopener noreferrer nofollow">{esc(title)}</a>'
                f'<span class="meta">{esc(_host(page_.url))} &middot; Retrieved {esc(_date(page_.fetched_at))}</span></div></li>')

    cited = [(n, cited_pages[url]) for url, n in numbers.items() if url in cited_pages]
    others = [p for p in all_pages if p.url not in numbers]
    if cited or others:
        sources = '<div class="sources-panel">'
        if cited:
            sources += ('<p class="label">Sources cited above</p><ol class="sources">'
                        + "".join(source_item(n, p) for n, p in cited) + "</ol>")
        if others:
            sources += (f'<p class="label" style="margin-top:{"18px" if cited else "0"}">Other pages read for this project (not cited)</p>'
                        '<ol class="sources">' + "".join(source_item(None, p) for p in others) + "</ol>")
        sources += ('<p class="note">Dates are when Foundry fetched each page. Publication dates are not collected, '
                    "so none are shown.</p></div>")
    else:
        sources = ('<p class="note">Foundry has not fetched any page for this project, so no claim can be sourced yet.</p>')

    notes_html = ""
    if search_note:
        notes = _NOTE_SPLIT.split(search_note.strip())
        items = "".join(f"<li>{esc(n)}</li>" for n in notes if n.strip())
        tip = ""
        if "web search" in search_note.lower() and ("unavailable" in search_note.lower() or "failed" in search_note.lower()):
            tip = ('<p style="margin-top:10px"><strong>Tip:</strong> web search is not reachable right now. Choose '
                   "<em>Go back</em> to revise your brief and paste source URLs; Foundry reads those pages directly.</p>")
        notes_html = (
            '<div class="alert" style="margin-top:16px"><span class="icon orange">!</span><div>'
            '<p class="label" style="color:var(--orange-700)">Research notes:</p>'
            f'<ul class="points">{items}</ul>{tip}</div></div>'
        )

    evidence_block = (
        '<section class="block"><div class="block-head"><h2>Claims and evidence</h2>'
        '<span class="kind ev">Evidence</span></div>'
        '<p class="muted">A claim is Sourced only if the page was fetched in this run and contains the quote. '
        "Scores and the market definition above are the agents' judgement, not sourced facts.</p>"
        '<div class="legend">'
        f"<span>{pill('Sourced', 'purple')} quote found word for word on a page fetched in this run</span>"
        f"<span>{pill('ASSUMPTION', 'orange')} no fetched source supports it</span>"
        f"<span>{pill('Agent judgement', 'muted')} scores, market and critic review</span></div>"
        f'<div class="claims">{claim_cards}</div>{sources}{notes_html}</section>'
    )

    risks = "".join(f"<li>{esc(r)}</li>" for r in critic["risks"])
    kills = "".join(f"<li>{esc(k)}</li>" for k in critic["kill_criteria"])
    tweaks = "".join(
        f'<div class="tweak"><strong>{esc(t["change"])}</strong><p class="label">Reason:</p><p>{esc(t["reason"])}</p></div>'
        for t in critic["tweaks"]
    )
    critic_block = (
        '<section class="block"><div class="block-head"><h2>Critic review</h2>'
        '<span class="kind ai">Agent judgement</span></div><div class="crit-grid">'
        f'<div class="panel strong full"><h3 class="label">Strongest case:</h3><p>{esc(critic["strongest_case"])}</p></div>'
        f'<div class="panel"><h3 class="label">Risks</h3><ol class="icons">{risks}</ol></div>'
        f'<div class="panel"><h3 class="label">Kill criteria (stop if any is true)</h3><ul class="kill">{kills}</ul></div>'
        '<div class="callout orange full" style="margin-top:0"><span class="icon orange">&#9873;</span><div>'
        '<h3 class="label" style="color:var(--orange-700)">Cheapest test that could disprove the idea:</h3>'
        f'<p>{esc(critic["cheapest_disproving_test"])}</p></div></div>'
        f'<div class="panel full"><h3 class="label">Changes to consider (options, not applied)</h3><div class="tweaks">{tweaks}</div></div>'
        f'<div class="panel full"><h3 class="label">Critic\'s reasoning:</h3><p>{esc(critic["reason"])}</p></div>'
        "</div></section>"
    )
    return f'<div class="results">{head}{options_block}{market_block}{evidence_block}{critic_block}</div>'
