import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import FactSummary from "./FactSummary.vue";
import FactCorrectionForm from "./FactCorrectionForm.vue";
import FactConversation from "./FactConversation.vue";
import type { Candidate } from "../../types/platform";
import { changedValues, draftFor, friendlyApiError } from "./presentation";
const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("../../composables/api", () => ({ api: mocks.api }));
function candidate(overrides: Partial<Candidate> = {}): Candidate {
  return {
    id: 1,
    project_id: 1,
    project_name: "Школа",
    team_id: 1,
    manager_id: null,
    fact_type: "project",
    proposed_changes: {
      fact_type: "project",
      object_name: "Школа",
      company_name: "Заказчик",
      contract_amount: "1000.00",
      currency: "KZT",
      stage: "proposal_sent",
      confidence: 0.9,
      evidence: "Договор",
    },
    current_values: {
      contract_amount: "500.00",
      currency: "KZT",
      object_name: "Школа",
      company_name: "Заказчик",
    },
    status: "pending",
    base_version: 1,
    current_version: 1,
    confidence: 0.9,
    uncertainties: ["currency_ambiguous"],
    evidence: [
      { id: 1, quote: "Договор", source_id: 1, source_url: "/api/messages/1/" },
    ],
    review_reason: "",
    created_at: "2026-10-01T12:00:00Z",
    crm_resolution: {
      state: "not_found",
      revision: 1,
      checked_at: null,
      error_code: "",
      options: [],
    },
    ...overrides,
  };
}
afterEach(() => vi.clearAllMocks());
describe("Fact review presentation and forms", () => {
  it("shows Russian business fields and current versus proposed values without raw keys", () => {
    const wrapper = mount(FactSummary, { props: { item: candidate() } });
    expect(wrapper.text()).toContain("Сумма договора");
    expect(wrapper.text()).toContain("Сейчас в проекте");
    expect(wrapper.text()).toContain("500 KZT");
    expect(wrapper.text()).toContain("1 000 KZT");
    expect(wrapper.text()).toContain("Коммерческое предложение отправлено");
    expect(wrapper.text()).not.toContain("contract_amount");
    expect(wrapper.text()).not.toContain("proposal_sent");
    expect(wrapper.text()).toContain("Сверьте значения с перепиской");
  });
  it("submits only changed business fields and an explicit reason, preserving evidence", async () => {
    const wrapper = mount(FactCorrectionForm, {
      props: { item: candidate(), busy: false, canApprove: true },
    });
    const inputs = wrapper.findAll("input");
    await inputs[2]!.setValue("1250,50");
    await wrapper
      .get("textarea[placeholder]")
      .setValue("Уточнение в переписке");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("save")?.[0]).toEqual([
      { contract_amount: "1250.50" },
      "Уточнение в переписке",
    ]);
    expect(wrapper.find("textarea.font-mono").exists()).toBe(false);
    await wrapper.get('button[type="button"]').trigger("click");
    expect(wrapper.emitted("cancel")).toHaveLength(1);
  });
  it("requires a reason and valid money and keeps confirmation disabled without rights", () => {
    expect(() =>
      changedValues(candidate(), {
        ...draftFor(candidate()),
        contract_amount: "oops",
      }),
    ).toThrow("Сумма договора");
    const wrapper = mount(FactCorrectionForm, {
      props: { item: candidate(), busy: false, canApprove: false },
    });
    expect(
      wrapper.get('button[type="submit"]').attributes("disabled"),
    ).toBeDefined();
  });
  it("builds payment and commitment edits without changing type or evidence", () => {
    const payment = candidate({
      fact_type: "payment",
      proposed_changes: {
        amount: "50",
        currency: "KZT",
        payment_date: "2026-10-01",
        payment_kind: "increment",
        evidence: "50",
      },
    });
    expect(
      changedValues(payment, {
        ...draftFor(payment),
        amount: "75",
        payment_kind: "promise",
      }),
    ).toEqual({ amount: "75", payment_kind: "promise" });
    const commitment = candidate({
      fact_type: "commitment",
      proposed_changes: {
        commitment_text: "Позвонить",
        deadline_at: null,
        deadline_precision: "unknown",
      },
    });
    expect(
      changedValues(commitment, {
        ...draftFor(commitment),
        commitment_text: "Отправить предложение",
      }),
    ).toEqual({ commitment_text: "Отправить предложение" });
  });
  it("shows only a date when the deadline time is unknown, preserving unchanged source values", () => {
    const item = candidate({
      fact_type: "commitment",
      proposed_changes: {
        commitment_text: "Отправить список",
        deadline_at: "2026-08-20T12:00:00Z",
        deadline_precision: "date",
      },
    });
    const summary = mount(FactSummary, { props: { item } });
    expect(summary.text()).toContain("20.08.2026");
    expect(summary.findAll("tr").find(row => row.text().includes("Срок выполнения"))!.text()).not.toContain(":");
    expect(draftFor(item).deadline_at).toHaveLength(10);
    expect(changedValues(item, draftFor(item))).toEqual({});
    const form = mount(FactCorrectionForm, {
      props: { item, busy: false, canApprove: true },
    });
    expect(form.find('input[type="date"]').exists()).toBe(true);
    expect(form.find('input[type="datetime-local"]').exists()).toBe(false);
  });
  it("explains server validation and version conflicts in Russian", () => {
    expect(
      friendlyApiError(
        { fields: { amount: ["A valid number is required."] } },
        400,
      ),
    ).toContain("Сумма: проверьте число");
    expect(friendlyApiError({ code: "conflict" }, 409)).toContain(
      "Обновите список",
    );
    expect(friendlyApiError({ error: "Permission denied" }, 403)).toContain(
      "Недостаточно прав",
    );
    expect(
      friendlyApiError(
        { fields: { reason: ["This field may not be blank."] } },
        400,
      ),
    ).toContain("Причина: заполните");
  });
  it("loads conversation on demand, adds earlier messages and separates the saved AI snapshot", async () => {
    const source = {
      id: 3,
      sender_name: "Ирина",
      sent_at: "2026-10-01T12:00:00Z",
      received_at: "2026-10-01T12:00:00Z",
      content: "Согласовано",
      is_source: true,
      used_by_ai: false,
    };
    mocks.api
      .mockResolvedValueOnce({
        source,
        messages: [source],
        before: 3,
        after: null,
        ai_messages: [
          {
            ...source,
            id: 2,
            is_source: false,
            content: "Уточнение",
            partial: true,
            used_by_ai: true,
          },
        ],
        ai_next_page: null,
        coverage: "AI получил часть истории.",
        chat_name: "Продажи",
        history_available: true,
      })
      .mockResolvedValueOnce({
        source,
        messages: [
          { ...source, id: 1, is_source: false, content: "Начало обсуждения" },
        ],
        before: null,
        after: 1,
        ai_messages: [],
        ai_next_page: null,
      });
    const wrapper = mount(FactConversation, { props: { candidateId: 1 } });
    expect(mocks.api).not.toHaveBeenCalled();
    await wrapper.get("button").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("Исходное сообщение");
    const earlierButton = wrapper
      .findAll("button")
      .find((b) => b.text().includes("более ранние"))!;
    await earlierButton.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("Начало обсуждения");
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "Что видел AI")!
      .trigger("click");
    expect(wrapper.text()).toContain("Уточнение");
    expect(wrapper.text()).toContain("AI получил только часть текста");
    expect(wrapper.text()).not.toContain("Начало обсуждения");
  });
});
