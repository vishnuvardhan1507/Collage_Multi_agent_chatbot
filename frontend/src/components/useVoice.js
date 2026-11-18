import { useEffect, useRef, useState } from 'react';

export default function useVoice(setDraft, onError) {
  const [enabled, setEnabled] = useState(false);
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(null);
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(false);
  const activeUtterance = useRef(null);
  const recognition = useRef(null);
  const autoRead = useRef(false);
  const generation = useRef(0);
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canSpeak = 'speechSynthesis' in window;
  function stop() {
    generation.current += 1;
    recognition.current?.abort(); recognition.current = null;
    activeUtterance.current = null;
    pausedRef.current = false; setPaused(false);
    if (canSpeak) {
      window.speechSynthesis.cancel();
      // Cancel can leave the synthesis queue paused in some browsers.
      if (window.speechSynthesis.paused) window.speechSynthesis.resume();
    }
    setListening(false); setSpeaking(null);
  }
  function speak(text, id) {
    stop();
    if (!canSpeak) { onError('Read aloud is unavailable in this browser.'); return; }
    const version = generation.current;
    const utterance = new SpeechSynthesisUtterance(text.replace(/\|/g, ', ').replace(/[-:]{3,}/g, '').replace(/[*#`]/g, ''));
    utterance.lang = 'en-IN';
    utterance.onend = () => { if (version === generation.current) { activeUtterance.current = null; pausedRef.current = false; setPaused(false); setSpeaking(null); } };
    utterance.onerror = () => { if (version === generation.current) { stop(); onError('Audio playback could not complete.'); } };
    activeUtterance.current = utterance;
    setSpeaking(id);
    try { window.speechSynthesis.speak(utterance); }
    catch { stop(); onError('Audio playback could not start.'); }
  }
  function togglePause() {
    if (!canSpeak || !activeUtterance.current) return;
    try {
      if (pausedRef.current) window.speechSynthesis.resume();
      else window.speechSynthesis.pause();
      pausedRef.current = !pausedRef.current;
      setPaused(pausedRef.current);
    } catch { stop(); onError('Audio playback could not continue.'); }
  }
  function dictate(draft) {
    if (listening) { stop(); return; }
    stop();
    if (!Recognition) { onError('Voice typing is unavailable in this browser. Try Chrome or Edge.'); return; }
    const version = generation.current;
    const engine = new Recognition(); recognition.current = engine;
    engine.lang = 'en-IN'; engine.interimResults = true; engine.continuous = true;
    engine.onresult = event => {
      if (version !== generation.current) return;
      const words = Array.from(event.results).map(result => result[0].transcript).join(' ');
      setDraft(`${draft}${draft ? ' ' : ''}${words}`);
    };
    engine.onerror = event => { if (version === generation.current && event.error !== 'aborted') onError(event.error === 'not-allowed' ? 'Microphone permission was denied. Allow microphone access to use voice typing.' : 'Voice typing stopped. Please try again.'); };
    engine.onend = () => { if (version === generation.current) setListening(false); };
    try { engine.start(); setListening(true); } catch { onError('Could not start the microphone.'); }
  }
  useEffect(() => () => { autoRead.current = false; generation.current += 1; recognition.current?.abort(); if (canSpeak) window.speechSynthesis.cancel(); }, []);
  return { enabled: enabled || speaking !== null, listening, speaking, paused, togglePause, canSpeak, canDictate: !!Recognition, stop, speak: (text, id) => { autoRead.current = false; setEnabled(false); speak(text, id); }, dictate, readNewAnswer: (text, id) => { if (autoRead.current) speak(text, id); }, toggle: () => { const next = !(enabled || speaking !== null); stop(); autoRead.current = next; setEnabled(next); } };
}
