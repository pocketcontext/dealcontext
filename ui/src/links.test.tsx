import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ContentLink } from "./links";

describe("ContentLink", () => {
  it("opens http(s) links in a new tab without opener or referrer", () => {
    render(<ContentLink href="https://example.com/x">outside</ContentLink>);
    const link = screen.getByRole("link", { name: "outside" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });
  it("keeps in-app and mailto links in this tab", () => {
    render(
      <>
        <ContentLink href="#/records/record000000001">record</ContentLink>
        <ContentLink href="mailto:someone@example.com">mail</ContentLink>
      </>,
    );
    expect(screen.getByRole("link", { name: "record" })).not.toHaveAttribute(
      "target",
    );
    expect(screen.getByRole("link", { name: "mail" })).not.toHaveAttribute(
      "target",
    );
  });
});
