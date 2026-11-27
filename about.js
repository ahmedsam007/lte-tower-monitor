/* About Tower Monitor: a link in each page's header (or in an element marked data-about-link)
   that opens a small box with the version, the developer and their links. Served by the app as
   /about.js with the version filled in. The links open in the normal web browser via the app. */
(() => {
  "use strict";
  const VERSION = "__VERSION__";
  const ar = window.LANG === "ar";
  const S = ar ? {
    about: "حول التطبيق",
    title: "حول مراقب الأبراج",
    version: `الإصدار ${VERSION}`,
    developer: "تطوير أحمد أبو أمونة",
    website: "الموقع الإلكتروني",
    appStore: "متجر App Store",
    github: "GitHub",
    source: "مجاني ومفتوح المصدر (رخصة MIT)",
    close: "إغلاق",
    failed: "تعذّر فتح الرابط.",
  } : {
    about: "About",
    title: "About Tower Monitor",
    version: `Version ${VERSION}`,
    developer: "Developed by Ahmed Abuammouna",
    website: "Website",
    appStore: "App Store",
    github: "GitHub",
    source: "Free and open source (MIT license)",
    close: "Close",
    failed: "Could not open the link.",
  };
  const LINKS = [["website", S.website, "ahmedsam.com"], ["appstore", S.appStore, "apps.apple.com"],
                 ["github", S.github, "github.com/ahmedsam007"], ["source", S.source, "github.com/ahmedsam007/lte-tower-monitor"]];

  const css = document.createElement("style");
  css.textContent = `
.about-dlg{border:1px solid var(--border,#8884);border-radius:12px;padding:0;width:min(380px,calc(100vw - 32px));
  background:var(--surface,#fff);color:var(--text,#111);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.about-dlg::backdrop{background:rgba(0,0,0,.35)}
.about-dlg .in{padding:18px 20px 14px}
.about-dlg h2{font-size:17px;margin:0 0 2px;font-weight:650}
.about-dlg .v{color:var(--text2,#555);font-size:12.5px;margin:0 0 10px}
.about-dlg .dev{margin:0 0 10px}
.about-dlg ul{list-style:none;margin:0;padding:0}
.about-dlg li button{display:flex;width:100%;justify-content:space-between;align-items:center;gap:10px;font:inherit;
  text-align:start;background:none;border:0;border-top:1px solid var(--grid,#8882);padding:10px 0;color:var(--text,#111);cursor:pointer}
.about-dlg li button:hover .t{text-decoration:underline}
.about-dlg li .u{color:var(--text2,#666);font-size:12px;direction:ltr}
.about-dlg li .t{color:var(--s1,#2a78d6)}
.about-dlg .row{display:flex;justify-content:flex-end;margin-top:10px}
.about-dlg .close{font:inherit;font-size:13px;padding:6px 14px;border-radius:8px;cursor:pointer;
  border:1px solid var(--border,#8884);background:var(--surface,#fff);color:var(--text,#111)}
.about-dlg .err{color:var(--critical,#d03b3b);font-size:12.5px;margin:8px 0 0}`;
  document.head.append(css);

  const el = (tag, cls, text) => Object.assign(document.createElement(tag), cls ? {className: cls} : {}, text ? {textContent: text} : {});
  const dlg = el("dialog", "about-dlg");
  dlg.setAttribute("translate", "no");          // already in the page's language
  dlg.setAttribute("aria-label", S.title);
  const box = el("div", "in");
  const list = el("ul");
  const err = el("p", "err"); err.hidden = true;
  for (const [key, label, url] of LINKS) {
    const b = el("button");
    b.type = "button";
    b.append(el("span", "t", label), el("span", "u", url));
    b.addEventListener("click", async () => {
      err.hidden = true;
      try {
        const r = await fetch("/api/about/open", {method: "POST", headers: {"Content-Type": "application/json"},
                                                   body: JSON.stringify({link: key})});
        if (!r.ok || !(await r.json()).ok) throw new Error("not opened");
      } catch (e) { err.textContent = S.failed; err.hidden = false; }
    });
    const li = el("li"); li.append(b); list.append(li);
  }
  const close = el("button", "close", S.close);
  close.type = "button";
  close.addEventListener("click", () => dlg.close());
  const row = el("div", "row"); row.append(close);
  box.append(el("h2", "", S.title), el("p", "v", S.version), el("p", "dev", S.developer), list, err, row);
  dlg.append(box);
  dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });   // click outside closes it
  document.body.append(dlg);

  let link = document.querySelector("[data-about-link]");
  if (!link) {
    link = el("a", "sub");
    link.style.color = "var(--s1)";
    const header = document.querySelector("header");
    const picker = header && header.querySelector(".lang-pick");
    if (picker) header.insertBefore(link, picker); else if (header) header.append(link);
  }
  link.href = "#about";
  link.textContent = S.about;
  link.setAttribute("translate", "no");
  link.addEventListener("click", e => { e.preventDefault(); dlg.showModal(); });
  if (location.hash === "#about") dlg.showModal();
})();
