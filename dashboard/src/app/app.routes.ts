import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () =>
      import('./features/overview/overview.component').then(m => m.OverviewComponent),
  },
  {
    path: 'persona',
    loadComponent: () =>
      import('./features/persona/persona.component').then(m => m.PersonaComponent),
  },
  {
    path: 'timeline',
    loadComponent: () =>
      import('./features/timeline/timeline.component').then(m => m.TimelineComponent),
  },
  {
    path: 'observations',
    loadComponent: () =>
      import('./features/observations/observations.component').then(m => m.ObservationsComponent),
  },
  {
    path: 'takeover',
    loadComponent: () =>
      import('./features/takeover/takeover.component').then(m => m.TakeoverComponent),
  },
  {
    path: 'goal-intelligence',
    loadComponent: () =>
      import('./features/goal-intelligence/goal-intelligence.component').then(
        m => m.GoalIntelligenceComponent
      ),
  },
  {
    path: 'human-clone',
    loadComponent: () =>
      import('./features/human-clone/human-clone.component').then(m => m.HumanCloneComponent),
  },
  {
    path: 'episodes',
    loadComponent: () =>
      import('./features/episodes/episodes.component').then(m => m.EpisodesComponent),
  },
  {
    path: 'workflow-templates',
    loadComponent: () =>
      import('./features/workflow-templates/workflow-templates.component').then(
        m => m.WorkflowTemplatesComponent
      ),
  },
  {
    path: 'retrieval-context',
    loadComponent: () =>
      import('./features/retrieval-context/retrieval-context.component').then(
        m => m.RetrievalContextComponent
      ),
  },
  {
    path: 'memory-rules',
    loadComponent: () =>
      import('./features/memory-rules/memory-rules.component').then(m => m.MemoryRulesComponent),
  },
  {
    path: 'graph',
    loadComponent: () =>
      import('./features/graph/graph.component').then(m => m.GraphComponent),
  },
  {
    path: 'patterns',
    loadComponent: () =>
      import('./features/patterns/patterns.component').then(m => m.PatternsComponent),
  },
  {
    path: 'health',
    loadComponent: () =>
      import('./features/health/health.component').then(m => m.HealthComponent),
  },
  {
    path: 'settings',
    loadComponent: () =>
      import('./features/settings/settings.component').then(m => m.SettingsComponent),
  },
  {
    path: 'export-center',
    loadComponent: () =>
      import('./features/export-center/export-center.component').then(m => m.ExportCenterComponent),
  },
  { path: '**', redirectTo: '' },
];
