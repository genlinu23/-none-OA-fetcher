export type Dict = Record<string, any>;

export type PreviewRow = {
  idx?: number;
  publisher?: string;
  doi?: string;
  url?: string;
};

export type ResearchRecord = {
  provider_id?: string;
  display_name?: string;
  year?: string | number;
  doi?: string;
  title?: string;
  venue?: string;
  oa_layer?: "oa" | "non_oa" | "unknown" | string;
  oa_status?: string;
  pdf_url?: string;
  landing_page_url?: string;
};

export type ResultRow = {
  publisher?: string;
  status?: string;
  doi?: string;
  pdf_path?: string;
};

export type AgentTurn = {
  role: "user" | "agent";
  text: string;
  includeInBrief?: boolean;
  normalizedRequirement?: string;
  taskNameHint?: string;
};

export type AppState = Dict & {
  preview_rows?: PreviewRow[];
  results?: Dict & { rows?: ResultRow[] };
  research?: Dict & {
    agent_config?: Dict;
    records?: ResearchRecord[];
    records_oa?: ResearchRecord[];
    records_non_oa?: ResearchRecord[];
    records_unknown_oa?: ResearchRecord[];
    include_terms?: string[];
    provider_stats?: Dict[];
    doi_files?: Dict;
    oa_summary?: Dict;
  };
  run?: Dict;
  progress?: Dict;
};
