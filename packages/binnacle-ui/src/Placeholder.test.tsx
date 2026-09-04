import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Placeholder } from "./Placeholder";

describe("Placeholder", () => {
  it("renders, proving the React + TypeScript + Vitest toolchain is wired correctly", () => {
    render(<Placeholder />);
    expect(screen.getByTestId("binnacle-ui-placeholder")).toBeInTheDocument();
  });
});
