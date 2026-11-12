// Chat bubbles. The user bubble is a navy pill aligned right; the
// assistant message is the H monogram avatar + content on paper.

import type { ReactNode } from "react";

import { Monogram } from "./Brand";

export function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-navy px-4 py-3 text-[15px] font-medium leading-snug text-white sm:max-w-[640px] sm:px-5">
        {text}
      </div>
    </div>
  );
}

export function AssistantBubble({ children }: { children: ReactNode }) {
  return (
    <div className="flex animate-fade-up gap-3">
      <div className="hidden flex-shrink-0 sm:block">
        <Monogram size={36} />
      </div>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}
