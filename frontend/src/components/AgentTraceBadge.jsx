import { Bot, CheckCircle2, Database, FileSearch, Route, Shield, Waypoints } from "lucide-react";

const icons = {
  guardrail: Shield,
  supervisor: Route,
  sql_query_agent: Database,
  sql_query_agent_execute: Database,
  validator_agent: CheckCircle2,
  rag_tool: FileSearch,
  db_tool: Database,
  memory_write: Waypoints
};

export default function AgentTraceBadge({ trace = [] }) {
  if (!trace.length) return null;
  return (
    <div className="trace" aria-label="Agent trace">
      {trace.map((step, index) => {
        const Icon = icons[step] || Bot;
        return (
          <span className="trace-step" title={step.replaceAll("_", " ")} key={`${step}-${index}`}>
            <Icon size={13} />
            <span>{step.replaceAll("_", " ")}</span>
          </span>
        );
      })}
    </div>
  );
}
