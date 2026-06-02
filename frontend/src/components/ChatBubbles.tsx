// Chat bubbles. The user bubble is a navy pill aligned right; the
// assistant message is the H monogram avatar + plain text on paper.

import type { ReactNode } from "react";

import { Monogram } from "./Brand";

interface UserBubbleProps {
  text: string;
}

export function UserBubble({ text }: UserBubbleProps) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[640px] rounded-2xl bg-navy px-5 py-3 text-[15px] font-medium leading-snug text-white">
        {text}
      </div>
    </div>
  );
}

interface AssistantBubbleProps {
  children: ReactNode;
}

export function AssistantBubble({ children }: AssistantBubbleProps) {
  return (
    <div className="flex gap-3">
      <div className="flex-shrink-0">
        <Monogram size={40} />
      </div>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

interface MarkdownishProps {
  text: string;
}

/** Minimal **bold** + paragraph renderer so the mocked answer reads
 *  the way the brief expects. No external dep — the answers we ship
 *  through /qa are short markdown and we want zero supply-chain risk. */
export function Markdownish({ text }: MarkdownishProps) {
  const paragraphs = text.split(/\n{2,}/);
  return (
    <div className="space-y-3 text-[15.5px] leading-relaxed text-ink">
      {paragraphs.map((p, i) => (
        <p key={i}>{renderInline(p)}</p>
      ))}
    </div>
  );
}

function renderInline(text: string): ReactNode {
  // Split into segments by **bold** markers.
  const parts: Array<{ bold: boolean; text: string }> = [];
  let cursor = 0;
  const re = /\*\*([^*]+)\*\*/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    if (match.index > cursor) {
      parts.push({ bold: false, text: text.slice(cursor, match.index) });
    }
    parts.push({ bold: true, text: match[1] });
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) parts.push({ bold: false, text: text.slice(cursor) });
  return parts.map((p, i) =>
    p.bold ? (
      <strong key={i} className="font-bold text-ink">
        {p.text}
      </strong>
    ) : (
      <span key={i}>{p.text}</span>
    ),
  );
}
