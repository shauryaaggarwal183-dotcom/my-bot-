from __future__ import annotations
import discord
from datetime import datetime, timezone


def _avatar_url(user: discord.abc.User) -> str:
    return str(user.display_avatar.with_size(64).url) if hasattr(user, 'display_avatar') else ''


def _escape(text: str) -> str:
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def build_html(
    guild: discord.Guild,
    channel: discord.TextChannel,
    ticket_data: dict,
    messages: list[discord.Message],
    opener: discord.Member | None,
    closer: discord.Member | None,
) -> str:
    category = ticket_data.get('category', 'Unknown')
    ticket_id = ticket_data.get('id', 0)
    created_at = ticket_data.get('created_at', '')
    closed_at = ticket_data.get('closed_at', datetime.now(timezone.utc).isoformat())
    answers: dict = ticket_data.get('answers', {})

    opener_name   = opener.display_name  if opener  else 'Unknown'
    opener_avatar = _avatar_url(opener)  if opener  else ''
    closer_name   = closer.display_name  if closer  else 'Unknown'
    closer_avatar = _avatar_url(closer)  if closer  else ''

    answers_html = ''
    if answers:
        rows = ''.join(
            f'<tr><td class="q-key">{_escape(q)}</td><td>{_escape(a)}</td></tr>'
            for q, a in answers.items() if a
        )
        answers_html = f'''
        <div class="answers-block">
            <h3>📋 Ticket Responses</h3>
            <table class="answers-table">{rows}</table>
        </div>'''

    messages_html = ''
    prev_author_id = None
    for msg in messages:
        if not msg.content and not msg.embeds and not msg.attachments:
            continue
        is_bot = msg.author.bot
        same_author = msg.author.id == prev_author_id
        prev_author_id = msg.author.id

        avatar_html = ''
        if not same_author:
            av = _avatar_url(msg.author)
            avatar_html = f'<img class="avatar" src="{_escape(av)}" alt="" />'

        name_html = ''
        if not same_author:
            role_color = '#BB8FCE'
            if hasattr(msg.author, 'top_role') and msg.author.top_role.color.value:
                role_color = f'#{msg.author.top_role.color.value:06x}'
            badge = ' <span class="badge bot">BOT</span>' if is_bot else ''
            name_html = f'<span class="author" style="color:{role_color}">{_escape(msg.author.display_name)}</span>{badge}'

        content_html = f'<p>{_escape(msg.content)}</p>' if msg.content else ''

        embed_html = ''
        for emb in msg.embeds:
            emb_title = _escape(emb.title or '')
            emb_desc  = _escape(emb.description or '')
            emb_color = f'#{emb.color.value:06x}' if emb.color else '#9B59B6'
            fields_html = ''.join(
                f'<div class="emb-field"><strong>{_escape(f.name)}</strong><p>{_escape(f.value)}</p></div>'
                for f in emb.fields
            )
            embed_html += (
                f'<div class="embed" style="border-left:4px solid {emb_color}">'
                f'{"<h4>" + emb_title + "</h4>" if emb_title else ""}'
                f'{"<p class=emb-desc>" + emb_desc + "</p>" if emb_desc else ""}'
                f'{fields_html}</div>'
            )

        attach_html = ''.join(
            f'<a class="attach" href="{_escape(a.url)}" target="_blank">📎 {_escape(a.filename)}</a>'
            for a in msg.attachments
        )

        ts = msg.created_at.strftime('%d %b %Y, %H:%M')
        indent = 'grouped' if same_author else ''
        messages_html += f'''
        <div class="msg {indent}">
            <div class="msg-avatar">{avatar_html}</div>
            <div class="msg-body">
                <div class="msg-header">{name_html}<span class="ts">{ts}</span></div>
                {content_html}{embed_html}{attach_html}
            </div>
        </div>'''

    generated_at = datetime.now(timezone.utc).strftime('%d %b %Y at %H:%M UTC')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Ticket #{ticket_id} — {_escape(guild.name)}</title>
<style>
  :root{{
    --bg:#0d0d1a;--surface:#13132b;--surface2:#1a1a35;
    --purple:#9B59B6;--purple-light:#BB8FCE;--purple-dark:#6C3483;
    --text:#e8e8f0;--muted:#8888aa;--border:#2a2a4a;
    --green:#2ECC71;--red:#E74C3C;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;font-size:15px;line-height:1.6}}
  a{{color:var(--purple-light);text-decoration:none}}
  .header{{background:linear-gradient(135deg,var(--purple-dark),var(--purple));padding:32px 40px;display:flex;align-items:center;gap:20px}}
  .header img{{width:72px;height:72px;border-radius:50%;border:3px solid rgba(255,255,255,.3)}}
  .header-info h1{{font-size:22px;font-weight:700}}
  .header-info p{{color:rgba(255,255,255,.75);font-size:13px;margin-top:4px}}
  .meta{{background:var(--surface);padding:20px 40px;display:flex;flex-wrap:wrap;gap:24px;border-bottom:1px solid var(--border)}}
  .meta-item{{display:flex;flex-direction:column;gap:2px}}
  .meta-item span:first-child{{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}}
  .meta-item span:last-child{{font-size:14px;font-weight:600}}
  .answers-block{{background:var(--surface2);margin:24px 40px;border-radius:12px;padding:20px;border:1px solid var(--border)}}
  .answers-block h3{{font-size:14px;color:var(--purple-light);margin-bottom:12px}}
  .answers-table{{width:100%;border-collapse:collapse}}
  .answers-table td{{padding:8px 12px;border-bottom:1px solid var(--border);font-size:14px}}
  .q-key{{color:var(--muted);width:200px;font-weight:600}}
  .messages{{padding:24px 40px 60px}}
  .msg{{display:flex;gap:14px;margin-bottom:2px;padding:3px 0}}
  .msg:not(.grouped){{margin-top:16px}}
  .msg-avatar{{width:40px;flex-shrink:0}}
  .msg-avatar img.avatar{{width:40px;height:40px;border-radius:50%}}
  .grouped .msg-avatar{{opacity:0}}
  .msg-header{{display:flex;align-items:baseline;gap:10px;margin-bottom:3px}}
  .author{{font-weight:700;font-size:15px}}
  .badge{{font-size:10px;padding:1px 5px;border-radius:4px;font-weight:600}}
  .badge.bot{{background:#5865F2;color:#fff}}
  .ts{{font-size:11px;color:var(--muted)}}
  .msg-body p{{color:var(--text);line-height:1.6}}
  .embed{{background:var(--surface2);border-radius:6px;padding:12px 16px;margin-top:6px;max-width:520px}}
  .embed h4{{font-size:14px;font-weight:700;color:var(--purple-light);margin-bottom:6px}}
  .emb-desc{{font-size:13px;color:var(--text)}}
  .emb-field{{margin-top:8px}}
  .emb-field strong{{font-size:12px;color:var(--muted);display:block}}
  .attach{{display:inline-block;margin-top:6px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:4px 10px;font-size:13px}}
  .footer-bar{{background:var(--surface);border-top:1px solid var(--border);padding:16px 40px;font-size:12px;color:var(--muted);display:flex;justify-content:space-between}}
  @media(max-width:600px){{
    .header,.meta,.messages,.answers-block{{padding-left:16px;padding-right:16px}}
    .answers-block{{margin-left:0;margin-right:0}}
  }}
</style>
</head>
<body>

<div class="header">
  {'<img src="' + _escape(str(guild.icon.url)) + '" alt="Server Icon"/>' if guild.icon else ''}
  <div class="header-info">
    <h1>🎫 Ticket #{ticket_id}</h1>
    <p>{_escape(guild.name)}  •  #{_escape(channel.name)}  •  {_escape(category)}</p>
  </div>
</div>

<div class="meta">
  <div class="meta-item"><span>Status</span><span style="color:var(--red)">Closed</span></div>
  <div class="meta-item"><span>Opened By</span><span>{_escape(opener_name)}</span></div>
  <div class="meta-item"><span>Closed By</span><span>{_escape(closer_name)}</span></div>
  <div class="meta-item"><span>Created</span><span>{_escape(str(created_at)[:16])}</span></div>
  <div class="meta-item"><span>Closed</span><span>{_escape(str(closed_at)[:16])}</span></div>
  <div class="meta-item"><span>Messages</span><span>{len(messages)}</span></div>
</div>

{answers_html}

<div class="messages">
{messages_html}
</div>

<div class="footer-bar">
  <span>Transcript generated {generated_at}</span>
  <span>Mine FrontTicket System</span>
</div>
</body>
</html>'''
