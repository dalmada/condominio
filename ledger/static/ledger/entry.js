(() => {
  const form = document.querySelector("[data-entry-form]");
  if (!form) return;
  const rule = form.elements.rule;
  const category = form.elements.category;
  const rows = Array.from(form.querySelectorAll("[data-code]"));
  const defaultsNode = document.getElementById("entry-category-defaults");
  const defaults = defaultsNode ? JSON.parse(defaultsNode.textContent) : {};

  const readings = form.querySelectorAll('[name^="previous_"], [name^="current_"], [name^="hot_previous_"], [name^="hot_current_"]');

  for (const input of readings) {
    input.addEventListener("input", () => {
      if (!input.hasAttribute("pattern")) return;
      const places = Number(input.dataset.readingPlaces);
      const whole = Number(input.dataset.readingWhole);
      const value = input.value;
      const parts = value.match(/^([0-9]+)[,.]([0-9]*)$/);
      if (parts) {
        const integer = parts[1].replace(/^0+(?=[0-9])/, "");
        if (integer.length <= whole && parts[2].length <= places) {
          input.value = integer.padStart(whole, "0") + "," + parts[2];
        }
        return;
      }
      const digits = value.replace(/[^0-9]/g, "");
      input.value = digits.length > whole
        ? digits.slice(0, whole) + "," + digits.slice(whole)
        : digits;
    });
  }

  const gasSummary = form.querySelector("[data-gas-summary]");
  const reais = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  const cubicMeters = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 3 });

  function updateGasPreview() {
    if (!gasSummary) return;
    const active = defaults[category.value]?.gas_tariff && rule.value === "measured";
    gasSummary.hidden = !active;
    const tariff = BigInt(gasSummary.dataset.priceCents);
    const complete = [];
    let participants = 0;
    for (const row of rows) {
      const output = row.querySelector("[data-gas-unit]");
      const selected = active && row.querySelector('input[type="checkbox"]').checked;
      output.hidden = !selected;
      if (!selected) continue;
      participants++;
      const previous = row.querySelector('[name^="previous_"]').value;
      const current = row.querySelector('[name^="current_"]').value;
      if (![previous, current].every((value) => /^[0-9]{5},[0-9]{3}$/.test(value))) {
        output.textContent = "Aguardando leituras";
        continue;
      }
      const usage = BigInt(current.replace(",", "")) - BigInt(previous.replace(",", ""));
      if (usage < 0n) {
        output.textContent = "Confira as leituras";
        continue;
      }
      complete.push({ code: row.dataset.code, output, usage, cents: (usage * tariff + 500n) / 1000n });
    }

    const ready = participants > 0 && complete.length === participants;
    const volume = complete.reduce((sum, row) => sum + row.usage, 0n);
    const total = (volume * tariff + 500n) / 1000n;
    if (ready && volume > 0n) {
      // Match the server's largest-remainder split of the total in cents.
      for (const row of complete) {
        row.cents = total * row.usage / volume;
        row.remainder = total * row.usage % volume;
      }
      const remaining = total - complete.reduce((sum, row) => sum + row.cents, 0n);
      complete.sort((a, b) => {
        if (a.remainder !== b.remainder) return a.remainder > b.remainder ? -1 : 1;
        return a.code < b.code ? -1 : 1;
      });
      for (const row of complete.slice(0, Number(remaining))) row.cents++;
    }
    for (const row of complete) {
      row.output.textContent = `${cubicMeters.format(Number(row.usage) / 1000)} m³ · ${reais.format(Number(row.cents) / 100)}`;
    }
    gasSummary.querySelector("[data-gas-total]").textContent = ready
      ? reais.format(Number(total) / 100)
      : "Aguardando leituras das unidades participantes";
  }

  function updateFields() {
    const water = category && defaults[category.value]?.water;
    const metered = ["measured", "mixed"].includes(rule.value);
    form.querySelectorAll("[data-for-rule]").forEach((group) => {
      group.hidden = !group.dataset.forRule.split(" ").includes(rule.value) ||
        (group.hasAttribute("data-water-only") && !water);
      const row = group.closest("[data-code]");
      const excluded =
        row && !row.querySelector('input[type="checkbox"]').checked;
      group.querySelectorAll("input,select,textarea").forEach((input) => {
        input.disabled = group.hidden || !!excluded;
      });
    });
    if (category) {
      const usesTariff = defaults[category.value]?.gas_tariff &&
        ["measured", "mixed"].includes(rule.value);
      form.querySelector("[data-manual-amount]").hidden = usesTariff;
      form.elements.amount.disabled = usesTariff;
      for (const input of readings) {
        const mask = metered && (usesTariff || water);
        if (!input.name.startsWith("hot_")) {
          input.labels[0].textContent = water
            ? (input.name.startsWith("previous_") ? "Água fria · anterior (m³)" : "Água fria · atual (m³)")
            : (input.name.startsWith("previous_") ? "Leitura anterior (m³)" : "Leitura atual (m³)");
        }
        input.inputMode = mask ? "numeric" : "decimal";
        const whole = water && !input.name.startsWith("hot_") ? 4 : 5;
        input.placeholder = mask ? (water ? "0".repeat(whole) + ",00" : "00001,764") : "";
        if (mask) {
          const places = water ? 2 : 3;
          input.dataset.readingPlaces = places;
          input.dataset.readingWhole = whole;
          input.pattern = `[0-9]{${whole}},[0-9]{${places}}`;
          input.title = water ? `${whole} dígitos, vírgula e dois decimais.` : "Cinco dígitos, vírgula e três decimais.";
        } else {
          input.removeAttribute("pattern");
          input.removeAttribute("title");
        }
      }
    }
    updateGasPreview();
  }

  form.addEventListener("input", updateGasPreview);

  if (category) {
    category.addEventListener("change", () => {
      const preset = defaults[category.value];
      if (preset) {
        rule.replaceChildren(
          ...(preset.choices.length === 1 ? [] : [new Option("Escolha o critério", "")]),
          ...preset.choices.map(([value, label]) => new Option(label, value)),
        );
        rule.value = preset.rule;
        form.elements.supplier.value = preset.supplier;
        rows.forEach((row) => {
          const code = row.dataset.code;
          row.querySelector('input[type="checkbox"]').checked =
            Object.prototype.hasOwnProperty.call(preset.shares, code);
          row.querySelector('[name^="percentage_"]').value =
            preset.shares[code] ?? "";
        });
      }
      updateFields();
    });
  }
  rule.addEventListener("change", updateFields);
  rows.forEach((row) =>
    row
      .querySelector('input[type="checkbox"]')
      .addEventListener("change", updateFields),
  );
  updateFields();
})();
