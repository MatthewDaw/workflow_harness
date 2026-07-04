/* ============================================================
   Verified Learning explainer — the "video" engine.
   Plays slides like a video: speaks each slide's data-narration
   (Web Speech API), shows it as a caption, and auto-advances
   when narration ends. Falls back to a timed reading speed if
   speech synthesis is unavailable or muted.
   ============================================================ */
(function () {
  "use strict";

  var deck = document.getElementById("deck");
  var slides = Array.prototype.slice.call(deck.querySelectorAll(".slide"));
  var caption = document.getElementById("caption");
  var counter = document.getElementById("counter");
  var titleEl = document.getElementById("title");
  var bar = document.getElementById("progressBar");
  var btnPlay = document.getElementById("btnPlay");
  var btnPrev = document.getElementById("btnPrev");
  var btnNext = document.getElementById("btnNext");
  var btnRestart = document.getElementById("btnRestart");
  var btnMute = document.getElementById("btnMute");
  var startOverlay = document.getElementById("startOverlay");
  var startBtn = document.getElementById("startBtn");

  var synth = window.speechSynthesis || null;
  var voice = null;
  var idx = 0;
  var playing = false;
  var muted = false;
  var token = 0;        // invalidates stale narration callbacks
  var timer = null;

  // ---- voice selection -------------------------------------------------
  function pickVoice() {
    if (!synth) return;
    var vs = synth.getVoices() || [];
    if (!vs.length) return;
    var pref = /(Samantha|Aria|Jenny|Google US English|Microsoft (Zira|Aria|Jenny)|Serena|Allison)/i;
    voice =
      vs.filter(function (v) { return /^en[-_]US/i.test(v.lang) && pref.test(v.name); })[0] ||
      vs.filter(function (v) { return /^en[-_]US/i.test(v.lang); })[0] ||
      vs.filter(function (v) { return /^en/i.test(v.lang); })[0] ||
      vs[0] || null;
  }
  if (synth) { pickVoice(); synth.onvoiceschanged = pickVoice; }

  // ---- helpers ---------------------------------------------------------
  function narrationOf(i) { return (slides[i].getAttribute("data-narration") || "").trim(); }
  function titleOf(i) { return slides[i].getAttribute("data-title") || ""; }

  function estimateMs(text) {
    var words = text ? text.split(/\s+/).length : 0;
    return Math.max(3200, (words / 2.7) * 1000 + 800); // ~160 wpm + padding
  }

  function clearTimer() { if (timer) { clearTimeout(timer); timer = null; } }
  function cancelSpeech() { try { if (synth) synth.cancel(); } catch (e) {} }

  function render(i) {
    slides.forEach(function (s, j) { s.classList.toggle("active", j === i); });
    idx = i;
    var text = narrationOf(i);
    caption.textContent = text;
    caption.classList.toggle("show", !!text);
    counter.textContent = (i + 1) + " / " + slides.length;
    titleEl.textContent = titleOf(i);
    bar.style.width = ((i + 1) / slides.length * 100) + "%";
  }

  // ---- narration + auto-advance ---------------------------------------
  function speak() {
    token++;
    var my = token;
    var text = narrationOf(idx);
    clearTimer();
    cancelSpeech();

    var advanced = false;
    function advance() {
      if (advanced || my !== token || !playing) return;
      advanced = true;
      if (idx < slides.length - 1) go(idx + 1);
      else stop(true);
    }

    if (synth && !muted && text) {
      var u = new SpeechSynthesisUtterance(text);
      if (voice) u.voice = voice;
      u.rate = 1.0; u.pitch = 1.02; u.volume = 1.0;
      u.onend = advance;
      u.onerror = advance;
      // give the engine a tick (Chrome can drop an immediate speak after cancel)
      setTimeout(function () { if (my === token && playing) { try { synth.speak(u); } catch (e) { advance(); } } }, 60);
      // safety net in case onend never fires
      timer = setTimeout(advance, estimateMs(text) + 5000);
    } else {
      timer = setTimeout(advance, estimateMs(text));
    }
  }

  function go(i) {
    i = Math.max(0, Math.min(slides.length - 1, i));
    render(i);
    if (playing) speak();
  }

  function play() {
    playing = true;
    btnPlay.textContent = "❚❚";
    btnPlay.setAttribute("aria-label", "Pause");
    speak();
  }
  function pause() {
    playing = false;
    token++;            // invalidate pending advance
    clearTimer();
    cancelSpeech();
    btnPlay.textContent = "▶";
    btnPlay.setAttribute("aria-label", "Play");
  }
  function toggle() { playing ? pause() : play(); }

  function stop(atEnd) {
    playing = false;
    clearTimer();
    cancelSpeech();
    btnPlay.textContent = atEnd ? "↺" : "▶";
    btnPlay.setAttribute("aria-label", atEnd ? "Replay" : "Play");
  }

  function restart() { pause(); render(0); play(); }

  // ---- controls --------------------------------------------------------
  btnPlay.addEventListener("click", function () {
    if (!playing && idx === slides.length - 1) { render(0); play(); }
    else toggle();
  });
  btnPrev.addEventListener("click", function () { go(idx - 1); });
  btnNext.addEventListener("click", function () { go(idx + 1); });
  btnRestart.addEventListener("click", restart);
  btnMute.addEventListener("click", function () {
    muted = !muted;
    btnMute.textContent = muted ? "🔇" : "🔊";
    btnMute.setAttribute("aria-label", muted ? "Unmute narration" : "Mute narration");
    if (playing) speak(); // restart timing under the new mode
  });

  document.addEventListener("keydown", function (e) {
    if (e.code === "Space") { e.preventDefault(); btnPlay.click(); }
    else if (e.code === "ArrowRight") { go(idx + 1); }
    else if (e.code === "ArrowLeft") { go(idx - 1); }
    else if (e.key === "r" || e.key === "R") { restart(); }
    else if (e.key === "m" || e.key === "M") { btnMute.click(); }
  });

  // pause if the tab is hidden (browsers throttle/garble speech anyway)
  document.addEventListener("visibilitychange", function () { if (document.hidden && playing) pause(); });

  // ---- boot ------------------------------------------------------------
  render(0);
  startBtn.addEventListener("click", function () {
    startOverlay.classList.add("hide");
    // unlock speech on the user gesture
    if (synth) { try { synth.cancel(); } catch (e) {} pickVoice(); }
    setTimeout(play, 250);
  });
})();
