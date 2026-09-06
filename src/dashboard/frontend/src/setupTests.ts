// Registers @testing-library/jest-dom matchers with vitest's `expect`.
// The `/vitest` subpath is the officially supported integration entrypoint
// in jest-dom v6+ and avoids relying on jest globals being present.
import "@testing-library/jest-dom/vitest";
