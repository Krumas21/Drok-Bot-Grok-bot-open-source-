// Live agent screens: noVNC over Drok's authenticated /vnc/<screen> WebSocket bridge.
import RFB from "https://cdn.jsdelivr.net/gh/novnc/noVNC@v1.6.0/core/rfb.js";

function connect(el) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const rfb = new RFB(el, `${proto}://${location.host}/vnc/${el.dataset.vnc}`);
  rfb.viewOnly = true;
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.background = "transparent";
  el.rfb = rfb;
  const state = el.querySelector(".vnc-state");
  rfb.addEventListener("connect", () => el.classList.add("live"));
  rfb.addEventListener("disconnect", () => {
    el.classList.remove("live");
    if (el.dataset.leaving) return;
    if (state) state.textContent = "screen offline - reconnecting…";
    setTimeout(() => connect(el), 4000);
  });
}

document.querySelectorAll("[data-vnc]").forEach(connect);
addEventListener("pagehide", () => document.querySelectorAll("[data-vnc]").forEach(el => {
  el.dataset.leaving = "1";
  el.rfb?.disconnect();
}));

// Take over: give the owner mouse + keyboard on that screen until they hand it back.
document.querySelectorAll("[data-takeover]").forEach(btn => {
  btn.addEventListener("click", () => {
    const el = document.querySelector(`[data-vnc="${btn.dataset.takeover}"]`);
    if (!el?.rfb) return;
    el.rfb.viewOnly = !el.rfb.viewOnly;
    el.closest(".bezel")?.classList.toggle("takeover", !el.rfb.viewOnly);
    btn.textContent = el.rfb.viewOnly ? "Take over" : "Release control";
    if (!el.rfb.viewOnly) el.rfb.focus();
  });
});
