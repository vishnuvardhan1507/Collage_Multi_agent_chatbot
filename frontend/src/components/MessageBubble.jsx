import { GraduationCap, Square, Volume2, Pause, Play } from 'lucide-react';
import Avatar from './Avatar';
import { useEffect, useState } from 'react';

function splitTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isTableLine(line) {
  return line.trim().startsWith("|") && line.includes("|", 1);
}

function isSeparatorLine(line) {
  return /^\s*\|?[\s:-]*-{3,}[\s|:-]*\|?\s*$/.test(line);
}

function MessageContent({ content }) {
  const lines = String(content || "").split(/\r?\n/);
  const blocks = [];
  let index = 0;

  while (index < lines.length) {
    if (isTableLine(lines[index])) {
      const tableLines = [];
      while (index < lines.length && isTableLine(lines[index])) {
        tableLines.push(lines[index]);
        index += 1;
      }
      const rows = tableLines.filter((line) => !isSeparatorLine(line)).map(splitTableRow);
      blocks.push({ type: "table", rows });
      continue;
    }

    const textLines = [];
    while (index < lines.length && !isTableLine(lines[index])) {
      textLines.push(lines[index]);
      index += 1;
    }
    blocks.push({ type: "text", text: textLines.join("\n") });
  }

  return (
    <>
      {blocks.map((block, blockIndex) => {
        if (block.type === "table" && block.rows.length > 0) {
          const [header, ...rows] = block.rows;
          return (
            <div className="message-table-wrap" key={`table-${blockIndex}`}>
              <table className="message-table">
                <thead>
                  <tr>
                    {header.map((cell, cellIndex) => (
                      <th key={`head-${cellIndex}`}>{cell}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, rowIndex) => (
                    <tr key={`row-${rowIndex}`}>
                      {header.map((_, cellIndex) => (
                        <td key={`cell-${cellIndex}`}>{row[cellIndex] || ""}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }

        return (
          <div className="message-text" key={`text-${blockIndex}`}>
            {block.text}
          </div>
        );
      })}
    </>
  );
}

export default function MessageBubble({ message, avatar, onRead, speaking, paused, onPause, canSpeak, onDecision, confirmationBusy, actionsDisabled }) {
  const isUser = message.role === "user";
  const [now, setNow] = useState(Date.now());
  const confirmation = message.confirmation;
  useEffect(() => {
    if (confirmation?.status !== 'pending') return;
    const timer = setTimeout(() => setNow(Date.now()), Math.max(0, Date.parse(confirmation.expires_at) - Date.now()) + 50);
    return () => clearTimeout(timer);
  }, [confirmation?.id, confirmation?.status, confirmation?.expires_at]);
  const status = confirmation?.status === 'pending' && Date.parse(confirmation.expires_at) <= now ? 'expired' : confirmation?.status;
  return (
    <article className={`message ${isUser ? "message-user" : "message-assistant"}`}>
      <div className="message-avatar">{isUser ? <Avatar avatar={avatar} /> : <GraduationCap />}</div>
      <div className="message-card"><div className="message-content">
        <MessageContent content={message.content} />
      </div>
      {!isUser && confirmation && <div className="chat-confirmation" aria-live="polite">
        {status === 'pending' ? <><span>Awaiting your confirmation · expires in 15 minutes</span><div className="confirmation-buttons"><button className="confirm-action" disabled={actionsDisabled || confirmationBusy} onClick={() => onDecision(confirmation.id, 'confirm')}>{confirmationBusy ? 'Processing…' : 'Confirm'}</button><button className="cancel-action" disabled={actionsDisabled || confirmationBusy} onClick={() => onDecision(confirmation.id, 'cancel')}>Cancel</button></div></> : <><strong>{({confirmed:'Confirmed', cancelled:'Cancelled', expired:'Expired', failed:'Failed', superseded:'Replaced by a newer request'})[status] || 'Unavailable'}</strong>{confirmation.result_message && <p>{confirmation.result_message}</p>}</>}
      </div>}
      <div className="message-footer">{message.timestamp && <time dateTime={message.timestamp}>{new Date(message.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>}
      {!isUser && <div className="read-aloud-controls" role="group" aria-label="Read-aloud controls">
        {speaking && <button type="button" className="read-aloud" onClick={onPause} disabled={!canSpeak} aria-label={paused ? 'Resume reading' : 'Pause reading'}>{paused ? <Play size={15} aria-hidden="true" /> : <Pause size={15} aria-hidden="true" />}{paused ? 'Resume' : 'Pause'}</button>}
        <button type="button" className="read-aloud" onClick={onRead} disabled={!canSpeak} aria-label={speaking ? 'Stop reading' : 'Read answer aloud'}>{speaking ? <Square size={15} aria-hidden="true" /> : <Volume2 size={15} aria-hidden="true" />}{speaking ? 'Stop' : 'Read aloud'}</button>
      </div>}</div>
      </div>
    </article>
  );
}
