PRD

The organization currently uses 15-Five for weekly planning, but it has no structural connection between individual weekly commitments and organizational strategic goals. Managers lack visibility into how team members' weekly work maps to Rally Cries, Defining Objectives, and Outcomes, making it impossible to identify misalignment until it's too late. The challenge is to build a production-ready micro-frontend module that replaces 15-Five with a system that enforces this connection through a complete weekly lifecycle: commit entry, prioritization, reconciliation, and manager review.

Problem & Context

Business Context

Today, weekly planning happens in 15-Five and is disconnected from strategic execution tracking. Employees fill out weekly plans with no enforced link to company objectives, and managers review them without knowing whether the work actually supports the right priorities. The desired state is a single system where every weekly commitment maps to a specific Supporting Outcome in the RCDO hierarchy, giving both ICs and managers real-time visibility into strategic alignment. This directly impacts execution discipline across 175+ employees.

Impact Metrics

Strategic alignment visibility (% of commits linked to RCDO), weekly planning completion rate, reconciliation accuracy (planned vs. actual), manager review turnaround time, time-to-plan reduction vs. 15-Five

Requirements & Success Criteria

Functional Requirements

Weekly commit CRUD with RCDO hierarchy linking, chess layer for categorization and prioritization, full weekly lifecycle state machine (DRAFT → LOCKED → RECONCILING → RECONCILED → Carry Forward), reconciliation view comparing planned vs. actual, manager dashboard with team roll-up, micro-frontend integration into existing PA host app following the PM remote pattern

Performance Benchmarks

API response times under 200ms for plan retrieval, lazy-loaded routes for sub-second initial render, Module Federation remote bundle size optimized for CDN delivery, pagination support (Spring Data Pageable) for team views with up to 2000 records

Code Quality Expectations

TypeScript strict mode, JaCoCo 80% minimum backend coverage, Vitest unit tests for all components, Cypress E2E with Cucumber/Gherkin BDD syntax, ESLint 9 + Prettier 3.3 (frontend), Spotless + SpotBugs (backend), all entities extend AbstractAuditingEntity, RTK Query for all API calls with cache invalidation

Time Constraints

1 week

Technical Contact

Yes

Technology

Required Languages

TypeScript (strict mode), Java 21, SQL

Dev Tools

React 18, Vite 5 with Module Federation, Spring Boot 3.3, Redux Toolkit with RTK Query, Flowbite React, Tailwind CSS, Vitest, React 18, Vite 5 with Module Federation, Spring Boot 3.3, Redux Toolkit with RTK Query, Flowbite React, Tailwind CSS, Vitest, Playwright

Cloud Platforms

AWS (EKS, CloudFront CDN, S3, SQS/SNS)

Other Requirements

PostgreSQL 16.4, Hibernate/JPA with Spring Data, Flyway migrations, Auth0 (OAuth2 JWT), Yarn Workspaces + Nx monorepo, Micro-frontend architecture (Vite Module Federation host/remote pattern), Outlook Graph API integration 

Off-Limits Tech

No CSS Modules or styled-components — use Tailwind CSS utility classes. Use RTK Query for all API data fetching — no Redux Saga or Thunk. Backend must use Spring Data JPA with Hibernate — no Prisma, TypeORM, or Sequelize. Use @Getter/@Setter/@Builder Lombok annotations, not @Data. No SSR frameworks (Next.js, Remix) — this is a client-side SPA.

In production, WC is a Vite Module Federation remote loaded by the PA host app. Your project should run standalone but be structured so it could be exposed as a remote — single route entry point, shared dependencies declared, no hardcoded shell/navigation. PA uses LogRocket + Loki for monitoring and Yarn Workspaces + Nx for package management; you don't need to replicate those for this assessment.