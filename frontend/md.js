/* Минимальный markdown-рендерер: интерфейс должен работать без интернета,
   поэтому никаких CDN и внешних библиотек. */
(function (global) {
  "use strict";

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function inline(text) {
    var out = escapeHtml(text);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    out = out.replace(/(^|\s)(https?:\/\/[^\s<]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    return out;
  }

  function isDivider(line) {
    return /^\|[\s:\-|]+\|$/.test(line.trim());
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|")
      .map(function (cell) { return cell.trim(); });
  }

  function render(markdown) {
    var lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
    var html = [];
    var paragraph = [];
    var listType = null;
    var inCode = false;
    var codeLines = [];
    var index = 0;

    function flushParagraph() {
      if (paragraph.length) {
        html.push("<p>" + inline(paragraph.join(" ")) + "</p>");
        paragraph = [];
      }
    }

    function flushList() {
      if (listType) {
        html.push("</" + listType + ">");
        listType = null;
      }
    }

    function openList(type) {
      if (listType !== type) {
        flushList();
        html.push("<" + type + ">");
        listType = type;
      }
    }

    while (index < lines.length) {
      var line = lines[index];
      var trimmed = line.trim();

      var fence = trimmed.match(/^```([A-Za-z0-9_+\-]*)\s*$/);
      if (fence) {
        if (inCode) {
          html.push("<pre>" + escapeHtml(codeLines.join("\n")) + "</pre>");
          codeLines = [];
          inCode = false;
        } else {
          flushParagraph();
          flushList();
          inCode = true;
        }
        index += 1;
        continue;
      }

      if (inCode) {
        codeLines.push(line);
        index += 1;
        continue;
      }

      if (!trimmed) {
        flushParagraph();
        flushList();
        index += 1;
        continue;
      }

      if (trimmed.charAt(0) === "|" && index + 1 < lines.length && isDivider(lines[index + 1])) {
        flushParagraph();
        flushList();
        var header = splitRow(trimmed);
        var rows = [];
        var cursor = index + 2;
        while (cursor < lines.length && lines[cursor].trim().charAt(0) === "|") {
          rows.push(splitRow(lines[cursor]));
          cursor += 1;
        }
        var table = ["<table><thead><tr>"];
        header.forEach(function (cell) { table.push("<th>" + inline(cell) + "</th>"); });
        table.push("</tr></thead><tbody>");
        rows.forEach(function (row) {
          table.push("<tr>");
          header.forEach(function (_, columnIndex) {
            table.push("<td>" + inline(row[columnIndex] || "") + "</td>");
          });
          table.push("</tr>");
        });
        table.push("</tbody></table>");
        html.push(table.join(""));
        index = cursor;
        continue;
      }

      var heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flushParagraph();
        flushList();
        var level = Math.min(heading[1].length + 1, 4);
        html.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
        index += 1;
        continue;
      }

      if (/^[-*]\s+/.test(trimmed)) {
        flushParagraph();
        openList("ul");
        html.push("<li>" + inline(trimmed.replace(/^[-*]\s+/, "")) + "</li>");
        index += 1;
        continue;
      }

      if (/^\d+[.)]\s+/.test(trimmed)) {
        flushParagraph();
        openList("ol");
        html.push("<li>" + inline(trimmed.replace(/^\d+[.)]\s+/, "")) + "</li>");
        index += 1;
        continue;
      }

      if (/^>\s?/.test(trimmed)) {
        flushParagraph();
        flushList();
        html.push("<blockquote>" + inline(trimmed.replace(/^>\s?/, "")) + "</blockquote>");
        index += 1;
        continue;
      }

      if (/^(-{3,}|\*{3,})$/.test(trimmed)) {
        flushParagraph();
        flushList();
        html.push("<hr>");
        index += 1;
        continue;
      }

      paragraph.push(trimmed);
      index += 1;
    }

    if (inCode) {
      html.push("<pre>" + escapeHtml(codeLines.join("\n")) + "</pre>");
    }
    flushParagraph();
    flushList();
    return html.join("\n");
  }

  global.HermesMarkdown = { render: render, escapeHtml: escapeHtml };
})(window);
