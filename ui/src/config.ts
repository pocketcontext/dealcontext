export interface Entity {
  table: string;
  label: string;
  title: string[];
  subtitle?: string[];
  search: string[];
  filters?: Record<string, string[]>;
  relations?: Record<string, string>;
  hidden?: string[];
  markdown?: string[];
  menu?: boolean;
  relationLabels?: Record<string, string>;
  reverseLabels?: Record<string, string>;
}
export const app: {
  name: string;
  authCollection: string;
  google?: boolean;
  entities: Entity[];
} = {
  name: "DealContext",
  authCollection: "agents",
  entities: [
    {
      table: "deals",
      label: "Deals",
      title: ["title"],
      search: ["title"],
      subtitle: ["status", "currency", "value_minor"],
      filters: {
        status: ["open", "won", "lost"],
      },
      relations: {
        organization: "organizations",
        person: "people",
        stage: "stages",
        owner: "agent_directory",
        created_by: "agent_directory",
        updated_by: "agent_directory",
      },
      markdown: ["lost_reason"],
    },
    {
      table: "organizations",
      label: "Organizations",
      title: ["name"],
      search: ["name", "address"],
      relations: {
        owner: "agent_directory",
        created_by: "agent_directory",
        updated_by: "agent_directory",
      },
    },
    {
      table: "people",
      label: "People",
      title: ["name"],
      search: ["name", "email", "job_title"],
      subtitle: ["email", "job_title"],
      relations: {
        organization: "organizations",
        owner: "agent_directory",
        created_by: "agent_directory",
        updated_by: "agent_directory",
      },
    },
    {
      table: "activities",
      label: "Activities",
      title: ["subject"],
      search: ["subject", "description"],
      subtitle: ["kind", "due_at"],
      filters: {
        done: ["0", "1"],
      },
      relations: {
        deal: "deals",
        person: "people",
        organization: "organizations",
        owner: "agent_directory",
        created_by: "agent_directory",
        updated_by: "agent_directory",
      },
      markdown: ["description"],
    },
    {
      table: "notes",
      label: "Notes",
      title: ["body"],
      search: ["body"],
      relations: {
        deal: "deals",
        person: "people",
        organization: "organizations",
        owner: "agent_directory",
        created_by: "agent_directory",
        updated_by: "agent_directory",
      },
      markdown: ["body"],
    },
    {
      table: "messages",
      label: "Messages",
      title: ["body"],
      search: ["body"],
      subtitle: ["channel", "direction"],
      relations: {
        person: "people",
        owner: "agent_directory",
        created_by: "agent_directory",
        updated_by: "agent_directory",
      },
      markdown: ["body"],
    },
    {
      table: "pipelines",
      label: "Pipelines",
      title: ["name"],
      search: ["name"],
      relations: {
        created_by: "agent_directory",
        updated_by: "agent_directory",
      },
    },
    {
      table: "stages",
      label: "Stages",
      title: ["name"],
      search: ["name"],
      relations: {
        pipeline: "pipelines",
        created_by: "agent_directory",
        updated_by: "agent_directory",
      },
    },
    {
      table: "enquiries",
      label: "Enquiries",
      title: ["name"],
      search: ["name", "email", "details"],
      subtitle: ["status"],
      filters: {
        status: ["new", "qualified", "rejected", "spam"],
      },
      relations: {
        person: "people",
        deal: "deals",
        updated_by: "agent_directory",
      },
      markdown: ["details"],
    },
    {
      table: "agent_directory",
      label: "Colleagues",
      title: ["name"],
      search: ["name"],
      menu: false,
    },
  ],
};
