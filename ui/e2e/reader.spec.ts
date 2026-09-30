import { test, expect } from "@playwright/test";
import { app } from "../src/config";
const e = app.entities[0],
  target = Object.values(e.relations || {})
    .map((t) => app.entities.find((x) => x.table === t))
    .find((t) => t && t.menu !== false),
  field = Object.keys(e.relations || {}).find(
    (f) => e.relations![f] === target?.table,
  ),
  id = "record000000001",
  targetId = "related00000001";
const row = (i = 0) => ({
  id: i ? "record" + String(i + 1).padStart(9, "0") : id,
  ...Object.fromEntries(e.title.map((f) => [f, "Synthetic " + i])),
  ...Object.fromEntries(
    (e.markdown || []).map((f) => [
      f,
      "**Safe** [bad](javascript:alert(1)) <script>window.evil=true</script>",
    ]),
  ),
  ...(field ? { [field]: targetId } : {}),
});
test.beforeEach(async ({ page }) => {
  await page.route(
    "**/api/collections/" + app.authCollection + "/auth-with-password",
    (r) =>
      r.fulfill({
        json: {
          token:
            "test." +
            Buffer.from(JSON.stringify({ exp: 4102444800 })).toString(
              "base64url",
            ) +
            ".test",
          record: {
            id: "user00000000001",
            collectionName: app.authCollection,
            name: "Test",
          },
        },
      }),
  );
  await page.route("**/api/context/query", (r) => {
    const sql = r.request().postDataJSON().sql;
    let rows: Record<string, unknown>[] = [];
    if (sql.includes('FROM "' + e.table + '"'))
      rows = sql.includes("OFFSET 30")
        ? [row(31)]
        : sql.includes("instr(") && sql.includes("missing")
          ? []
          : sql.includes("WHERE id=")
            ? [row()]
            : Array.from({ length: 31 }, (_, i) => row(i));
    else if (
      target &&
      sql.includes('FROM "' + target.table + '"') &&
      sql.includes("WHERE id=")
    )
      rows = [
        {
          id: targetId,
          ...Object.fromEntries(
            target.title.map((f) => [f, "Related example"]),
          ),
        },
      ];
    const columns = rows.length ? Object.keys(rows[0]) : [];
    return r.fulfill({
      json: {
        columns,
        rows: rows.map((v) => columns.map((c) => v[c])),
        truncated: false,
      },
    });
  });
});
async function login(page: any) {
  await page.getByLabel("Email", { exact: true }).fill("test@example.com");
  await page.getByLabel("Password", { exact: true }).fill("test-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}
test("deep links survive login and reload; related records are linked and Markdown is inert", async ({
  page,
}) => {
  await page.goto("/#/" + e.table + "/" + id);
  await login(page);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "Synthetic",
  );
  await page.reload();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "Synthetic",
  );
  await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0);
  expect(await page.evaluate(() => Boolean((window as any).evil))).toBe(false);
  if (target) {
    const link = page.locator(
      'main a[href="#/' + target.table + "/" + targetId + '"]',
    );
    await expect(link).toBeVisible();
    await link.click();
    await expect(page).toHaveURL(
      new RegExp("/" + target.table + "/" + targetId),
    );
    await page.goBack();
    await expect(page.getByRole("heading",{level:1})).toContainText("Synthetic");
  }
});
test("mobile collection search uses server pagination and handles empty results", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/" + e.table);
  await login(page);
  await page.getByRole("button", { name: "Browse", exact: true }).click();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.locator(".results")).toContainText("Synthetic 31");
  await page.getByRole("button", {name:"Previous",exact:true}).click();
  await page.getByRole("searchbox").fill("missing");
  await expect(page.getByText("No matching records.")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
