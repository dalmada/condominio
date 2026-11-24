import React from "react";
import { createRoot } from "react-dom/client";

const currency = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const money = (cents) => currency.format(cents / 100);

function UnitCards({ units }) {
  return <div className="tw:my-5 tw:grid tw:grid-cols-2 tw:gap-3 tw:lg:grid-cols-4">
    {units.map(({ code, name, href, cents, status }) =>
      <a key={code} href={href}
         className="tw:block tw:min-w-0 tw:rounded-xl tw:border tw:border-[#dbe1d9] tw:bg-white tw:px-4 tw:py-3 tw:hover:border-[#62947a]">
        <small>Apartamento {name}</small>
        <strong className="tw:block tw:text-lg tw:font-semibold tw:tracking-tight tw:tabular-nums">{money(cents)}</strong>
        {status && <small>{status}</small>}
      </a>
    )}
  </div>;
}

function ExpenseChart({ categories, gradient }) {
  return <>
    <h2>Despesas por categoria</h2>
    {categories.length ? <>
      <div aria-hidden="true" className="tw:mx-auto tw:my-5 tw:aspect-square tw:w-full tw:max-w-[230px] tw:rounded-full"
           style={{ background: `conic-gradient(${gradient})` }} />
      <ul className="tw:m-0 tw:list-none tw:p-0 tw:divide-y tw:divide-[#dbe1d9]">
        {categories.map(({ name, amount, color }) =>
          <li key={name} className="tw:grid tw:grid-cols-[10px_minmax(0,1fr)_auto] tw:items-center tw:gap-2 tw:py-2 tw:text-sm">
            <span aria-hidden="true" className="tw:size-2.5 tw:rounded-full" style={{ backgroundColor: color }} />
            <span>{name}</span><strong>{money(amount)}</strong>
          </li>
        )}
      </ul>
    </> : <p className="muted">As despesas lançadas aparecem aqui.</p>}
  </>;
}

const data = document.getElementById("patio-overview-data");
if (data) {
  const { units, categories, gradient } = JSON.parse(data.textContent);
  createRoot(document.getElementById("patio-unit-cards")).render(<UnitCards units={units} />);
  createRoot(document.getElementById("patio-expense-chart")).render(<ExpenseChart categories={categories} gradient={gradient} />);
}
