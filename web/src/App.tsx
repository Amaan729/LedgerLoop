import { useState } from "react";
import Overview from "./components/Overview";

const TABS = ["Overview"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [tab, setTab] = useState<Tab>("Overview");
  return (
    <>
      <header>
        <div>
          <h1>LedgerLoop</h1>
          <div className="sub">order-to-cash agents</div>
        </div>
        <nav>
          {TABS.map((t) => (
            <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </nav>
      </header>
      <main>{tab === "Overview" && <Overview />}</main>
    </>
  );
}
