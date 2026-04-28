(() => {
  const escapeHtml = (text) => text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");

  const colorClasses = {
    30: "ansi-black",
    31: "ansi-red",
    32: "ansi-green",
    33: "ansi-yellow",
    34: "ansi-blue",
    35: "ansi-magenta",
    36: "ansi-cyan",
    37: "ansi-white",
    90: "ansi-black",
    91: "ansi-red",
    92: "ansi-green",
    93: "ansi-yellow",
    94: "ansi-blue",
    95: "ansi-magenta",
    96: "ansi-cyan",
    97: "ansi-white",
  };

  function activeClasses(state) {
    const classes = [];
    if (state.bold) {
      classes.push("ansi-bold");
    }
    if (state.dim) {
      classes.push("ansi-dim");
    }
    if (state.italic) {
      classes.push("ansi-italic");
    }
    if (state.underline) {
      classes.push("ansi-underline");
    }
    if (state.color) {
      classes.push(state.color);
    }
    return classes;
  }

  function openSpan(state) {
    const classes = activeClasses(state);
    if (classes.length === 0) {
      return "";
    }
    return `<span class="${classes.join(" ")}">`;
  }

  function hasStyle(state) {
    return activeClasses(state).length > 0;
  }

  function applyCodes(state, codes) {
    for (const code of codes) {
      if (code === 0) {
        state.bold = false;
        state.dim = false;
        state.italic = false;
        state.underline = false;
        state.color = "";
      } else if (code === 1) {
        state.bold = true;
      } else if (code === 2) {
        state.dim = true;
      } else if (code === 3) {
        state.italic = true;
      } else if (code === 4) {
        state.underline = true;
      } else if (code === 22) {
        state.bold = false;
        state.dim = false;
      } else if (code === 23) {
        state.italic = false;
      } else if (code === 24) {
        state.underline = false;
      } else if (code === 39) {
        state.color = "";
      } else if (code in colorClasses) {
        state.color = colorClasses[code];
      }
    }
  }

  function render(text) {
    const state = { bold: false, dim: false, italic: false, underline: false, color: "" };
    let output = "";
    let open = false;
    let index = 0;
    const sgr = /\x1b\[([0-9;]*)m/g;
    for (const match of text.matchAll(sgr)) {
      output += escapeHtml(text.slice(index, match.index));
      if (open) {
        output += "</span>";
        open = false;
      }
      const codes = match[1] === "" ? [0] : match[1].split(";").map(Number);
      applyCodes(state, codes);
      if (hasStyle(state)) {
        output += openSpan(state);
        open = true;
      }
      index = match.index + match[0].length;
    }
    output += escapeHtml(text.slice(index));
    if (open) {
      output += "</span>";
    }
    return output;
  }

  window.rcloneRunnerAnsi = { render };
})();
