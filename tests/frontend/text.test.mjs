// Runs with Node's built-in test runner: `node --test tests/frontend`
// Exercises frontend/lib/text.js — the ACTUAL file reader.js and novel.js
// load in the browser (see backend/main.py _page()), not a copy.
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { splitParagraphs, stripTitleEcho, hasContent } = require("../../frontend/lib/text.js");

describe("splitParagraphs", () => {
  test("blank-line-separated prose keeps wrapped lines joined", () => {
    const got = splitParagraphs("Line one\nwrapped here.\n\nSecond para.");
    assert.deepEqual(got, ["Line one wrapped here.", "Second para."]);
  });

  test("one-paragraph-per-line source splits on every line", () => {
    // The bug this guards against: reader.js used to split ONLY on blank
    // lines and turn every remaining single \n into a space, so a
    // single-newline chapter (measured: 12 of 125 translated chapters,
    // across two different relay models) rendered as one wall of text.
    const got = splitParagraphs("A.\nB.\nC.\nD.\nE.\nF.");
    assert.equal(got.length, 6);
  });

  test("mostly single-newline text is treated as one-per-line", () => {
    const text = "x.\n".repeat(40) + "\ny.";
    assert.ok(splitParagraphs(text).length > 30);
  });

  test("no text is lost when re-splitting a real single-newline chapter", () => {
    const text = ("段落 " + "x".repeat(60) + "\n").repeat(50);
    const rejoined = splitParagraphs(text).join("").replace(/\s/g, "");
    assert.equal(rejoined, text.replace(/\s/g, ""));
  });

  test("markdown heading artifacts are stripped", () => {
    assert.equal(splitParagraphs("## Title\n\nBody.")[0], "Title");
  });

  test("empty input yields no paragraphs", () => {
    assert.deepEqual(splitParagraphs(""), []);
  });
});

describe("stripTitleEcho", () => {
  test("a short title does not eat real prose that happens to contain it", () => {
    // The bug: `first.includes(h1.slice(0, 24))` deleted "The fireplace
    // crackled as she stepped inside." for a chapter titled "Fire".
    const list = ["The fireplace crackled as she stepped inside.", "Second para."];
    assert.deepEqual(stripTitleEcho(list, "Fire"), list);
  });

  test("an exact title echo is still stripped", () => {
    assert.deepEqual(stripTitleEcho(["Fire", "Body text."], "Fire"), ["Body text."]);
  });

  test("a long title echoed with a numeric suffix is still stripped", () => {
    const list = ["The Price of Development 7", "Body."];
    assert.deepEqual(stripTitleEcho(list, "The Price of Development"), ["Body."]);
  });

  test("a 'Chapter N:' prefix on the title is ignored before comparing", () => {
    const list = ["The Long Road Home", "Body."];
    assert.deepEqual(stripTitleEcho(list, "Chapter 12: The Long Road Home"), ["Body."]);
  });

  test("long prose that merely STARTS with the title text is kept", () => {
    const list = [
      "The Price of Development had never felt so heavy to her as it did that grey morning.",
      "Body.",
    ];
    assert.deepEqual(stripTitleEcho(list, "The Price of Development"), list);
  });

  test("a single-paragraph chapter is never stripped down to nothing", () => {
    assert.deepEqual(stripTitleEcho(["Fire"], "Fire"), ["Fire"]);
  });

  test("an empty or numeric-only title is a no-op", () => {
    const list = ["The 7 gates opened.", "B"];
    assert.deepEqual(stripTitleEcho(list, ""), list);
    assert.deepEqual(stripTitleEcho(list, "Chapter 7"), list);
  });
});

describe("hasContent", () => {
  test("server-rendered shape uses has_content", () => {
    assert.equal(hasContent({ has_content: true }), true);
    assert.equal(hasContent({ has_content: false }), false);
  });

  test("JSON API shape uses original_content", () => {
    assert.equal(hasContent({ original_content: "text" }), true);
    assert.equal(hasContent({ original_content: null }), false);
    assert.equal(hasContent({ original_content: "" }), false);
  });

  test("is defensive against missing or empty input", () => {
    assert.equal(hasContent(undefined), false);
    assert.equal(hasContent(null), false);
    assert.equal(hasContent({}), false);
  });
});
