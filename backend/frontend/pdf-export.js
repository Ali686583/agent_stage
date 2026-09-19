// Export PDF (reponse unique ou discussion complete) : genere un VRAI PDF
// texte (titres/paragraphes/listes/tableaux/code reellement structures,
// via marked.lexer() + jsPDF/jsPDF-autoTable), jamais une capture d'ecran
// ni du texte tronque. N'importe quel echec ici (CDN bloque, contenu
// inattendu) doit rester local a l'action PDF : jamais faire planter le
// reste de la discussion (voir les try/catch cote appelant dans workspace.js).
(function () {
  "use strict";

  const MARGIN = 15;
  const LINE_HEIGHT = 5.6;
  const PAGE_WIDTH_MM = 210; // A4 portrait
  const PAGE_HEIGHT_MM = 297;
  const CONTENT_WIDTH = PAGE_WIDTH_MM - MARGIN * 2;
  const HEADING_SIZES = { 1: 18, 2: 15, 3: 13, 4: 11.5, 5: 10.5, 6: 10.5 };

  function extractInlineText(tokens) {
    if (!tokens) return "";
    return tokens
      .map((tok) => {
        if (tok.tokens) return extractInlineText(tok.tokens);
        return tok.text != null ? tok.text : tok.raw || "";
      })
      .join("");
  }

  function isFullyStrong(tokens) {
    return Array.isArray(tokens) && tokens.length === 1 && tokens[0].type === "strong";
  }

  function ensureSpace(doc, state, needed) {
    if (state.y + needed > PAGE_HEIGHT_MM - MARGIN) {
      doc.addPage();
      state.y = MARGIN;
    }
  }

  function writeParagraph(doc, text, state, opts) {
    opts = opts || {};
    const fontSize = opts.fontSize || 10.5;
    const style = opts.style || "normal";
    const indent = opts.indent || 0;
    if (!text) return;
    doc.setFont("helvetica", style);
    doc.setFontSize(fontSize);
    doc.setTextColor(0, 0, 0);
    const lines = doc.splitTextToSize(text, Math.max(20, CONTENT_WIDTH - indent));
    lines.forEach((line) => {
      ensureSpace(doc, state, LINE_HEIGHT);
      doc.text(line, MARGIN + indent, state.y);
      state.y += LINE_HEIGHT;
    });
    state.y += opts.spacingAfter != null ? opts.spacingAfter : 2;
  }

  function renderTokens(doc, tokens, state, indent) {
    indent = indent || 0;
    (tokens || []).forEach((token) => {
      switch (token.type) {
        case "heading": {
          const size = HEADING_SIZES[token.depth] || 11;
          state.y += 2;
          writeParagraph(doc, extractInlineText(token.tokens), state, {
            fontSize: size,
            style: "bold",
            indent,
            spacingAfter: 3,
          });
          break;
        }
        case "paragraph": {
          const text = extractInlineText(token.tokens);
          const style = isFullyStrong(token.tokens) ? "bold" : "normal";
          writeParagraph(doc, text, state, { indent, style, spacingAfter: 3 });
          break;
        }
        case "list": {
          (token.items || []).forEach((item, idx) => {
            const bullet = token.ordered ? `${(token.start || 1) + idx}.` : "-";
            const itemTokens = item.tokens || [];
            const flatText = itemTokens
              .map((it) => (it.tokens ? extractInlineText(it.tokens) : it.text || ""))
              .join(" ")
              .trim();
            writeParagraph(doc, `${bullet} ${flatText}`, state, { indent: indent + 5, spacingAfter: 1.5 });
          });
          state.y += 1.5;
          break;
        }
        case "table": {
          const head = [(token.header || []).map((c) => extractInlineText(c.tokens) || c.text || "")];
          const body = (token.rows || []).map((row) => row.map((c) => extractInlineText(c.tokens) || c.text || ""));
          renderAutoTable(doc, state, head, body, indent);
          break;
        }
        case "code": {
          renderCodeBlock(doc, state, token.text || "", indent);
          break;
        }
        case "blockquote": {
          renderTokens(doc, token.tokens, state, indent + 5);
          break;
        }
        case "hr": {
          ensureSpace(doc, state, 6);
          doc.setDrawColor(200, 200, 200);
          doc.line(MARGIN + indent, state.y, PAGE_WIDTH_MM - MARGIN, state.y);
          state.y += 5;
          break;
        }
        case "space":
          break;
        default: {
          const text = token.text || extractInlineText(token.tokens);
          if (text) writeParagraph(doc, text, state, { indent, spacingAfter: 2 });
        }
      }
    });
  }

  function renderAutoTable(doc, state, head, body, indent) {
    ensureSpace(doc, state, 20);
    try {
      doc.autoTable({
        head,
        body,
        startY: state.y,
        margin: { left: MARGIN + indent, right: MARGIN },
        styles: { fontSize: 9, cellPadding: 2 },
        headStyles: { fillColor: [109, 184, 49], textColor: [255, 255, 255] },
        theme: "grid",
      });
      state.y = doc.lastAutoTable.finalY + 4;
    } catch (error) {
      // Repli honnete : jamais faire echouer tout l'export pour un tableau
      // recalcitrant, une representation texte simple reste utile.
      (body || []).forEach((row) => writeParagraph(doc, row.join(" | "), state, { indent, fontSize: 9 }));
    }
  }

  function renderCodeBlock(doc, state, codeText, indent) {
    doc.setFont("courier", "normal");
    doc.setFontSize(8.5);
    const codeLines = doc.splitTextToSize(codeText, Math.max(20, CONTENT_WIDTH - indent - 4));
    const boxHeight = codeLines.length * 4.6 + 4;
    ensureSpace(doc, state, Math.min(boxHeight, PAGE_HEIGHT_MM - MARGIN * 2));
    doc.setFillColor(240, 240, 240);
    doc.rect(MARGIN + indent, state.y - 3.5, CONTENT_WIDTH - indent, Math.min(boxHeight, PAGE_HEIGHT_MM), "F");
    codeLines.forEach((line) => {
      ensureSpace(doc, state, 4.6);
      doc.text(line, MARGIN + indent + 2, state.y);
      state.y += 4.6;
    });
    doc.setFont("helvetica", "normal");
    state.y += 3;
  }

  function renderMarkdown(doc, markdownText, state, indent) {
    if (!window.marked || typeof window.marked.lexer !== "function") {
      writeParagraph(doc, markdownText || "", state, { indent });
      return;
    }
    let tokens;
    try {
      tokens = window.marked.lexer(markdownText || "");
    } catch (error) {
      writeParagraph(doc, markdownText || "", state, { indent });
      return;
    }
    renderTokens(doc, tokens, state, indent);
  }

  function renderStructuredBlock(doc, block, state, indent) {
    if (!block) return;
    switch (block.type) {
      case "markdown":
      case "text":
      case "callout":
        renderMarkdown(doc, block.content || "", state, indent);
        break;
      case "table":
        renderAutoTable(doc, state, [block.headers || []], block.rows || [], indent);
        break;
      case "code":
        renderCodeBlock(doc, state, block.content || "", indent);
        break;
      case "chart":
        writeParagraph(doc, `[Graphique${block.title ? " : " + block.title : ""} - non exporte en PDF]`, state, {
          indent,
          style: "italic",
          fontSize: 9,
        });
        break;
      case "image":
        writeParagraph(doc, `[Image${block.alt ? " : " + block.alt : ""}]`, state, { indent, style: "italic", fontSize: 9 });
        break;
      case "file":
        writeParagraph(doc, `[Piece jointe : ${block.name || ""}]`, state, { indent, style: "italic", fontSize: 9 });
        break;
      case "link":
        writeParagraph(doc, `${block.label || block.url || ""} (${block.url || ""})`, state, { indent });
        break;
      default:
        if (block.content) renderMarkdown(doc, block.content, state, indent);
    }
  }

  function renderMessageBody(doc, message, state, indent) {
    if (Array.isArray(message.blocks) && message.blocks.length) {
      message.blocks.forEach((block) => renderStructuredBlock(doc, block, state, indent));
    } else if (message.content) {
      renderMarkdown(doc, message.content, state, indent);
    }
  }

  function sanitizeFilename(name) {
    const cleaned = (name || "document")
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .replace(/[^a-zA-Z0-9-_ ]/g, "")
      .trim()
      .replace(/\s+/g, "-")
      .slice(0, 80);
    return cleaned || "document";
  }

  function getJsPdfCtor() {
    if (window.jspdf && window.jspdf.jsPDF) return window.jspdf.jsPDF;
    return null;
  }

  function downloadMessagePdf(message, options) {
    options = options || {};
    const JsPDF = getJsPdfCtor();
    if (!JsPDF) throw new Error("jsPDF non charge");
    const doc = new JsPDF({ unit: "mm", format: "a4" });
    const state = { y: MARGIN };
    doc.setFont("helvetica", "bold");
    doc.setFontSize(13);
    doc.text(options.title || message.authorName || "Reponse", MARGIN, state.y);
    state.y += 7;
    if (options.subtitle) {
      doc.setFont("helvetica", "normal");
      doc.setFontSize(9);
      doc.setTextColor(120, 120, 120);
      doc.text(options.subtitle, MARGIN, state.y);
      doc.setTextColor(0, 0, 0);
      state.y += 7;
    }
    doc.setDrawColor(220, 220, 220);
    doc.line(MARGIN, state.y, PAGE_WIDTH_MM - MARGIN, state.y);
    state.y += 6;
    renderMessageBody(doc, message, state, 0);
    doc.save(sanitizeFilename(options.filename || message.authorName || "reponse") + ".pdf");
  }

  function downloadConversationPdf(conversation, messages, options) {
    options = options || {};
    const JsPDF = getJsPdfCtor();
    if (!JsPDF) throw new Error("jsPDF non charge");
    const doc = new JsPDF({ unit: "mm", format: "a4" });
    const state = { y: MARGIN };
    doc.setFont("helvetica", "bold");
    doc.setFontSize(15);
    const titleLines = doc.splitTextToSize(conversation.title || "Discussion", CONTENT_WIDTH);
    titleLines.forEach((line) => {
      doc.text(line, MARGIN, state.y);
      state.y += 7;
    });
    state.y += 2;
    doc.setDrawColor(200, 200, 200);
    doc.line(MARGIN, state.y, PAGE_WIDTH_MM - MARGIN, state.y);
    state.y += 8;

    (messages || []).forEach((message) => {
      ensureSpace(doc, state, 14);
      const isUser = message.role === "user";
      // Distinction visuelle claire utilisateur / agent (mission §6) :
      // bandeau de fond leger + libelle en gras au-dessus de chaque message.
      doc.setFillColor(isUser ? 235 : 240, isUser ? 245 : 240, isUser ? 233 : 240);
      const labelY = state.y;
      doc.rect(MARGIN - 2, labelY - 4.2, CONTENT_WIDTH + 4, 6.5, "F");
      doc.setFont("helvetica", "bold");
      doc.setFontSize(9.5);
      if (isUser) doc.setTextColor(72, 121, 32);
      else doc.setTextColor(70, 70, 70);
      doc.text(String(message.authorName || (isUser ? "Utilisateur" : "Agent")), MARGIN, labelY);
      doc.setTextColor(0, 0, 0);
      state.y += 6;
      renderMessageBody(doc, message, state, 2);
      state.y += 4;
    });

    doc.save(sanitizeFilename(options.filename || conversation.title || "discussion") + ".pdf");
  }

  window.AgentStagePdf = { downloadMessagePdf, downloadConversationPdf };
})();
