"use strict";
/*
 * markdown.js - just enough Markdown to read a repo doc.
 *
 * No library: the UI has no bundler and no CDN, and pulling one in for this
 * would be the first network dependency in the whole project.
 *
 * SAFETY: the source is escaped BEFORE any rule runs, so nothing in a note
 * or a source file can put markup on the page. That matters here more than
 * usual - vault content is data JARVIS reads, never instructions or code it
 * runs (see CLAUDE.md's guardrail 7). Images are deliberately not loaded;
 * a remote <img> in someone's README would be a network call made on their
 * behalf, so the alt text is shown instead. Links only survive when they're
 * http/https/mailto, which rules out javascript: URLs.
 */

const MD_ALLOWED_LINK = /^(https?:|mailto:)/i;

function mdEscape(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* Inline rules, applied to already-escaped text. */
function mdInline(text) {
  return text
    // Code first: whatever is inside must not be touched by the rest.
    .replace(/`([^`]+)`/g, (_m, code) => `<code>${code}</code>`)
    .replace(/!\[([^\]]*)\]\((?:[^()\s]|\([^()]*\))*(?:\s[^)]*)?\)/g, (_m, alt) =>
      `<span class="md-img">🖼 ${alt || "image"}</span>`)
    .replace(/\[([^\]]+)\]\(((?:[^()\s]|\([^()]*\))+)(?:\s[^)]*)?\)/g, (_m, label, href) => {
      const clean = href.replace(/&amp;/g, "&");
      return MD_ALLOWED_LINK.test(clean)
        ? `<a href="${mdEscape(clean)}" target="_blank" rel="noopener">${label}</a>`
        // A relative path points at a file, not a page - show it, don't link it.
        : `<span class="md-path">${label}</span>`;
    })
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>");
}

function mdTableRow(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
}

function renderMarkdown(source) {
  const lines = mdEscape(source || "").split("\n");
  const out = [];
  let i = 0;

  const paragraph = (buf) => {
    if (buf.length) out.push(`<p>${mdInline(buf.join(" "))}</p>`);
    buf.length = 0;
  };
  const buffer = [];

  while (i < lines.length) {
    const line = lines[i];

    // fenced code
    const fence = line.match(/^\s*```+\s*([\w+-]*)\s*$/);
    if (fence) {
      paragraph(buffer);
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;                                  // closing fence
      const lang = fence[1] ? ` data-lang="${fence[1]}"` : "";
      out.push(`<pre${lang}><code>${body.join("\n")}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      paragraph(buffer);
      const level = heading[1].length;
      out.push(`<h${level}>${mdInline(heading[2])}</h${level}>`);
      i++;
      continue;
    }

    if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) {
      paragraph(buffer);
      out.push("<hr>");
      i++;
      continue;
    }

    // table: a header row plus a |---|---| separator
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length
        && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      paragraph(buffer);
      const head = mdTableRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        rows.push(mdTableRow(lines[i++]));
      }
      out.push(
        `<table><thead><tr>${head.map((c) => `<th>${mdInline(c)}</th>`).join("")}</tr></thead>`
        + `<tbody>${rows.map((r) =>
            `<tr>${r.map((c) => `<td>${mdInline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`
      );
      continue;
    }

    // blockquote
    if (/^\s*&gt;\s?/.test(line)) {
      paragraph(buffer);
      const body = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        body.push(lines[i++].replace(/^\s*&gt;\s?/, ""));
      }
      out.push(`<blockquote>${mdInline(body.join(" "))}</blockquote>`);
      continue;
    }

    // lists (one level; nested items are flattened rather than dropped)
    const bullet = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      paragraph(buffer);
      const ordered = /\d/.test(bullet[2]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!m) {
          // a wrapped continuation line belongs to the item above
          if (items.length && /^\s{2,}\S/.test(lines[i]) && lines[i].trim()) {
            items[items.length - 1] += " " + lines[i].trim();
            i++;
            continue;
          }
          break;
        }
        const task = m[3].match(/^\[([ xX])\]\s+(.*)$/);
        const indent = m[1].length >= 2 ? ' class="md-sub"' : "";
        items.push(task
          ? `<li${indent}><span class="md-task">${task[1] === " " ? "☐" : "☑"}</span> ${task[2]}`
          : `<li${indent}>${m[3]}`);
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((t) => mdInline(t) + "</li>").join("")}</${tag}>`);
      continue;
    }

    if (!line.trim()) {
      paragraph(buffer);
      i++;
      continue;
    }

    buffer.push(line);
    i++;
  }
  paragraph(buffer);
  return out.join("\n");
}

window.renderMarkdown = renderMarkdown;
