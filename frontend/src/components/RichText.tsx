// Renders an answer: a small, dependency-free Markdown subset (headings,
// lists, bold, italic, inline code) plus the part that matters here — every
// `[F2]` the advisor cites becomes a chip that jumps to that source card.

import type { ReactNode } from "react";

import type { Citation } from "../api";

interface RichTextProps {
  text: string;
  citations?: Citation[];
  onCite?: (fragmentId: string) => void;
}

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] };

const HEADING_RE = /^(#{1,4})\s+(.*)$/;
const BULLET_RE = /^\s*[-*•]\s+(.*)$/;
const ORDERED_RE = /^\s*\d+[.)]\s+(.*)$/;

function parseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length) blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
    paragraph = [];
  };

  for (const rawLine of text.split("\n")) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      flush();
      continue;
    }
    const heading = HEADING_RE.exec(line);
    if (heading) {
      flush();
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2] });
      continue;
    }
    const bullet = BULLET_RE.exec(line);
    const ordered = bullet ? null : ORDERED_RE.exec(line);
    if (bullet || ordered) {
      flush();
      const isOrdered = !bullet;
      const item = (bullet ?? ordered)![1];
      const last = blocks[blocks.length - 1];
      if (last?.kind === "list" && last.ordered === isOrdered) last.items.push(item);
      else blocks.push({ kind: "list", ordered: isOrdered, items: [item] });
      continue;
    }
    // An indented continuation line belongs to the previous list item.
    const last = blocks[blocks.length - 1];
    if (/^\s{2,}/.test(rawLine) && last?.kind === "list" && paragraph.length === 0) {
      last.items[last.items.length - 1] += ` ${line.trim()}`;
      continue;
    }
    paragraph.push(line.trim());
  }
  flush();
  return blocks;
}

// One pass over the inline syntax we support. Citations first so `[F1]`
// inside bold text still becomes a chip.
const INLINE_RE = /\[\s*(F\d+(?:\s*[,;y]\s*F\d+)*)\s*\]|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|`([^`]+)`/gi;

export function RichText({ text, citations = [], onCite }: RichTextProps) {
  const byId = new Map(citations.filter((c) => c.fragment_id).map((c) => [c.fragment_id as string, c]));

  function citeChip(rawId: string, key: string): ReactNode {
    const id = rawId.toUpperCase();
    const citation = byId.get(id);
    if (!citation) {
      // Cited but never retrieved: never render it as if it were a source.
      return (
        <span
          key={key}
          title="Esta referencia no corresponde a ninguna fuente recuperada"
          className="mx-0.5 rounded bg-risk-high-bg px-1 font-mono text-[11px] text-risk-high-fg line-through"
        >
          {id}
        </span>
      );
    }
    return (
      <button
        key={key}
        type="button"
        onClick={() => onCite?.(id)}
        title={`${citation.title}${citation.section && citation.section !== "general" ? ` · ${citation.section}` : ""}`}
        aria-label={`Ver la fuente ${id}: ${citation.title}`}
        className="ml-0.5 inline-flex translate-y-[-1px] items-center rounded bg-primary-soft px-1.5 py-px font-mono text-[11px] font-semibold text-primary-dark transition hover:bg-primary hover:text-white"
      >
        {id}
      </button>
    );
  }

  function inline(source: string, keyPrefix: string): ReactNode[] {
    const nodes: ReactNode[] = [];
    let cursor = 0;
    let n = 0;
    for (const match of source.matchAll(INLINE_RE)) {
      const index = match.index ?? 0;
      if (index > cursor) nodes.push(source.slice(cursor, index));
      const key = `${keyPrefix}-${n++}`;
      if (match[1]) {
        for (const id of match[1].match(/F\d+/gi) ?? []) nodes.push(citeChip(id, `${key}-${id}`));
      } else if (match[2]) {
        nodes.push(
          <strong key={key} className="font-semibold text-ink">
            {inline(match[2], key)}
          </strong>,
        );
      } else if (match[3]) {
        nodes.push(<em key={key}>{match[3]}</em>);
      } else if (match[4]) {
        nodes.push(
          <code key={key} className="rounded bg-line/70 px-1 py-px font-mono text-[0.9em]">
            {match[4]}
          </code>,
        );
      }
      cursor = index + match[0].length;
    }
    if (cursor < source.length) nodes.push(source.slice(cursor));
    return nodes;
  }

  return (
    <div className="space-y-3 text-[15.5px] leading-relaxed text-ink">
      {parseBlocks(text).map((block, i) => {
        const key = `b${i}`;
        if (block.kind === "heading") {
          return (
            <h3 key={key} className="pt-1 font-serif text-[17px] font-bold text-ink">
              {inline(block.text, key)}
            </h3>
          );
        }
        if (block.kind === "list") {
          const List = block.ordered ? "ol" : "ul";
          return (
            <List key={key} className={`space-y-1.5 pl-5 ${block.ordered ? "list-decimal" : "list-disc"} marker:text-muted`}>
              {block.items.map((item, j) => (
                <li key={`${key}-${j}`}>{inline(item, `${key}-${j}`)}</li>
              ))}
            </List>
          );
        }
        return <p key={key}>{inline(block.text, key)}</p>;
      })}
    </div>
  );
}
