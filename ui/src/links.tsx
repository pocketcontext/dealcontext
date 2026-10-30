import type { ReactNode } from "react";
// Outside links open in a new tab so the reader keeps its place; in-app and
// mailto links stay in this tab.
export function ContentLink({
  href,
  children,
}: {
  href?: string;
  children: ReactNode;
}) {
  return (
    <a
      href={href}
      rel="noopener noreferrer"
      {...(/^https?:/i.test(href || "") ? { target: "_blank" } : {})}
    >
      {children}
    </a>
  );
}
