import AgentTraceBadge from "./AgentTraceBadge.jsx";

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

export default function MessageBubble({ message }) {
  const isUser = message.role === "user";
  return (
    <article className={`message ${isUser ? "message-user" : "message-assistant"}`}>
      <div className="message-content">
        <MessageContent content={message.content} />
      </div>
      {!isUser && <AgentTraceBadge trace={message.trace} />}
    </article>
  );
}
