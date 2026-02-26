import { CommonModule } from '@angular/common';
import { AfterViewInit, Component, ElementRef, OnDestroy, OnInit, ViewChild } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import * as THREE from 'three';
import { ApiService } from '../../core/services/api.service';
import { FingerprintResponse, ObservationListResponse, PatternItem } from '../../core/models';

type TagCategory = 'behavior' | 'decision';
type TagSource = 'fingerprint' | 'observation' | 'pattern';

interface CloneTag {
  id: string;
  label: string;
  value: string;
  category: TagCategory;
  source: TagSource;
  color: string;
  priority: number;
}

interface TagNode {
  tag: CloneTag;
  node: THREE.Mesh;
  link: THREE.Line;
}

interface TagProjection {
  id: string;
  left: number;
  top: number;
  color: string;
  label: string;
  value: string;
  category: TagCategory;
  source: TagSource;
  priority: number;
  hidden: boolean;
  selected: boolean;
}

@Component({
  selector: 'app-human-clone',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="page-header">
      <h1>Human Clone Graph</h1>
      <p>3D representation of behavioral and decision profile tags from your timeline memory.</p>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
        <button class="btn btn-primary" (click)="reloadData()" [disabled]="loadingData">
          {{ loadingData ? 'Refreshing...' : 'Refresh tags' }}
        </button>
        <button class="btn" [class.btn-primary]="showBehavior" (click)="toggleCategory('behavior')">
          {{ showBehavior ? 'Behavior: on' : 'Behavior: off' }}
        </button>
        <button class="btn" [class.btn-primary]="showDecision" (click)="toggleCategory('decision')">
          {{ showDecision ? 'Decision: on' : 'Decision: off' }}
        </button>
        <span class="badge badge-accent">{{ behaviorCount }} behavioral</span>
        <span class="badge badge-warn">{{ decisionCount }} decision</span>
        <span class="badge badge-muted">{{ visibleNodeCount }} visible nodes</span>
        <span class="badge badge-muted">zoom: {{ zoomTier }}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px;">
        <span style="font-size:12px;color:var(--muted);">Label density</span>
        <button class="btn" [class.btn-primary]="labelMode==='overview'" (click)="setLabelMode('overview')">Overview</button>
        <button class="btn" [class.btn-primary]="labelMode==='balanced'" (click)="setLabelMode('balanced')">Balanced</button>
        <button class="btn" [class.btn-primary]="labelMode==='full'" (click)="setLabelMode('full')">Full</button>
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:8px;">
        <label style="font-size:12px;color:var(--muted);">Min confidence</label>
        <input
          type="range"
          min="40"
          max="95"
          step="1"
          [value]="minPriority"
          (input)="setMinPriority($any($event.target).value)"
        />
        <span style="font-size:12px;color:var(--muted);">{{ minPriority }}%</span>
        <button class="btn" [class.btn-primary]="showFingerprint" (click)="toggleSource('fingerprint')">Fingerprint</button>
        <button class="btn" [class.btn-primary]="showObservation" (click)="toggleSource('observation')">Observations</button>
        <button class="btn" [class.btn-primary]="showPattern" (click)="toggleSource('pattern')">Patterns</button>
      </div>
      <div style="font-size:12px;color:var(--muted);margin-top:8px;">
        Map intent: decision tags are positioned around the head/upper zone, behavior tags around core/action zones.
      </div>
      <div *ngIf="error" class="error-banner" style="margin-top:10px;">{{ error }}</div>
      <div *ngIf="notice" style="font-size:13px;color:var(--muted);margin-top:8px;">{{ notice }}</div>
    </div>

    <div class="card" style="position:relative;overflow:hidden;padding:0;min-height:640px;">
      <div
        #viewport
        style="
          position:relative;
          width:100%;
          min-height:640px;
          background:radial-gradient(1200px 600px at 50% 30%, #12253a 0%, #0a1624 45%, #050b12 100%);
        "
      >
        <canvas #canvas style="display:block;width:100%;height:640px;"></canvas>

        <div style="position:absolute;inset:0;pointer-events:none;">
          <div
            *ngFor="let tag of projectedTags"
            [style.left.px]="tag.left"
            [style.top.px]="tag.top"
            [style.opacity]="tag.hidden ? 0 : 1"
            style="
              position:absolute;
              transform:translate(-50%,-50%);
              background:rgba(10,16,24,0.88);
              border:1px solid rgba(255,255,255,0.12);
              border-left-width:3px;
              border-radius:6px;
              padding:6px 8px;
              min-width:120px;
              transition:opacity .1s linear;
              pointer-events:auto;
              cursor:pointer;
            "
            [style.border-left-color]="tag.color"
            [style.box-shadow]="tag.selected ? '0 0 0 1px rgba(255,255,255,0.25), 0 0 18px rgba(89,182,255,0.35)' : 'none'"
            (click)="selectTag(tag.id)"
          >
            <div style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;" [style.color]="tag.color">
              {{ tag.category }}
            </div>
            <div style="font-size:12px;font-weight:700;color:#e5edf7;">{{ tag.label }}</div>
            <div style="font-size:11px;color:#9fb3c8;">{{ tag.value }}</div>
          </div>
        </div>

        <div
          style="
            position:absolute;
            left:16px;
            top:16px;
            background:rgba(6,12,20,0.72);
            border:1px solid rgba(255,255,255,0.08);
            border-radius:8px;
            padding:10px;
            width:280px;
            color:#d5e4f3;
          "
        >
          <div style="font-weight:700;margin-bottom:8px;">How to read this graph</div>
          <div style="font-size:12px;line-height:1.5;color:#9fb3c8;">
            Upper/head zone: decision pressure and context. Core/action zone: stable behavior patterns.
            Click any tag node to inspect why it matters.
          </div>
          <div style="margin-top:10px;font-size:12px;color:#d3e2f0;">
            <div style="margin-bottom:4px;">Top signals right now:</div>
            <div *ngFor="let insight of topInsights; let i = index" style="display:flex;gap:6px;align-items:flex-start;margin-bottom:4px;">
              <span style="color:#7fc9ff;">{{ i + 1 }}.</span>
              <span>{{ insight.label }} — {{ insight.value }}</span>
            </div>
          </div>
        </div>

        <div
          style="
            position:absolute;
            right:16px;
            top:16px;
            background:rgba(6,12,20,0.72);
            border:1px solid rgba(255,255,255,0.08);
            border-radius:8px;
            padding:10px;
            width:260px;
            color:#d5e4f3;
          "
        >
          <div style="font-weight:700;margin-bottom:6px;">Legend</div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
            <span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#18c7bf;"></span>
            <span style="font-size:12px;">Behavioral profile nodes</span>
          </div>
          <div style="display:flex;align-items:center;gap:8px;">
            <span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#ff7b6b;"></span>
            <span style="font-size:12px;">Decision dynamics nodes</span>
          </div>
          <div style="margin-top:10px;font-size:11px;color:#8fa8c1;">
            Data sources: fingerprint, observations, patterns.
          </div>
        </div>

        <div
          *ngIf="selectedTag"
          style="
            position:absolute;
            right:16px;
            bottom:16px;
            background:rgba(6,12,20,0.82);
            border:1px solid rgba(255,255,255,0.1);
            border-radius:8px;
            padding:10px;
            width:320px;
            color:#d5e4f3;
          "
        >
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;">
            <div style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;" [style.color]="selectedTag.color">
              {{ selectedTag.category }}
            </div>
            <button class="btn" style="padding:2px 8px;font-size:11px;" (click)="clearSelectedTag()">Close</button>
          </div>
          <div style="font-size:14px;font-weight:700;margin-top:4px;">{{ selectedTag.label }}</div>
          <div style="font-size:12px;color:#9fb3c8;margin-top:2px;">{{ selectedTag.value }}</div>
          <div style="margin-top:8px;font-size:12px;line-height:1.45;color:#d0deea;">
            {{ explainSelectedTag(selectedTag) }}
          </div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:14px;">
      <div style="font-weight:700;margin-bottom:8px;">Plain-language interpretation</div>
      <div style="font-size:13px;color:var(--muted);line-height:1.5;">
        <div *ngIf="primaryBehaviorTag">
          <strong style="color:#18c7bf;">Behavior anchor:</strong>
          {{ primaryBehaviorTag.label }} ({{ primaryBehaviorTag.value }})
        </div>
        <div *ngIf="primaryDecisionTag" style="margin-top:4px;">
          <strong style="color:#ff7b6b;">Decision hotspot:</strong>
          {{ primaryDecisionTag.label }} ({{ primaryDecisionTag.value }})
        </div>
      </div>
      <div style="margin-top:10px;">
        <div style="font-size:12px;color:var(--muted);margin-bottom:6px;">What this means for execution</div>
        <ul style="margin:0;padding-left:18px;color:#d5e4f3;font-size:13px;">
          <li *ngFor="let line of recommendedActions">{{ line }}</li>
        </ul>
      </div>
    </div>
  `,
})
export class HumanCloneComponent implements OnInit, AfterViewInit, OnDestroy {
  @ViewChild('viewport', { static: true }) viewportRef!: ElementRef<HTMLDivElement>;
  @ViewChild('canvas', { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;

  loadingData = false;
  error: string | null = null;
  notice: string | null = null;
  projectedTags: TagProjection[] = [];
  showBehavior = true;
  showDecision = true;
  showFingerprint = true;
  showObservation = true;
  showPattern = true;
  minPriority = 55;
  labelMode: 'overview' | 'balanced' | 'full' = 'balanced';
  zoomTier: 'overview' | 'balanced' | 'detail' = 'balanced';
  selectedTagId: string | null = null;

  private scene: THREE.Scene | null = null;
  private camera: THREE.PerspectiveCamera | null = null;
  private renderer: THREE.WebGLRenderer | null = null;
  private humanRoot: THREE.Group | null = null;
  private ring: THREE.Mesh | null = null;
  private animationId: number | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private tagNodes: TagNode[] = [];
  private tagPool: CloneTag[] = [];
  private frameCounter = 0;

  constructor(private api: ApiService) {}

  get behaviorCount(): number {
    return this.tagPool.filter((tag) => tag.category === 'behavior').length;
  }

  get decisionCount(): number {
    return this.tagPool.filter((tag) => tag.category === 'decision').length;
  }

  get visibleNodeCount(): number {
    return this.projectedTags.filter((item) => !item.hidden).length;
  }

  get selectedTag(): CloneTag | null {
    if (!this.selectedTagId) return null;
    return this.tagPool.find((tag) => tag.id === this.selectedTagId) || null;
  }

  get topInsights(): CloneTag[] {
    return this.getRenderableTags()
      .slice()
      .sort((a, b) => b.priority - a.priority)
      .slice(0, 3);
  }

  get primaryBehaviorTag(): CloneTag | null {
    const item = this.tagPool
      .filter((tag) => tag.category === 'behavior')
      .slice()
      .sort((a, b) => b.priority - a.priority)[0];
    return item || null;
  }

  get primaryDecisionTag(): CloneTag | null {
    const item = this.tagPool
      .filter((tag) => tag.category === 'decision')
      .slice()
      .sort((a, b) => b.priority - a.priority)[0];
    return item || null;
  }

  get recommendedActions(): string[] {
    const actions: string[] = [];
    if (this.primaryBehaviorTag) {
      actions.push(`Keep communication aligned with ${this.primaryBehaviorTag.label.toLowerCase()} before proposing major changes.`);
    }
    if (this.primaryDecisionTag) {
      actions.push(`For high-stakes choices, explicitly address ${this.primaryDecisionTag.value.toLowerCase()} in plan rationale.`);
    }
    actions.push('Use selected decision tags as checklist items during takeover execution reports.');
    actions.push('Review top behavior tags first when explaining why a task strategy changed.');
    return actions.slice(0, 4);
  }

  ngOnInit(): void {
    this.reloadData();
  }

  ngAfterViewInit(): void {
    this.initScene();
    this.attachResize();
  }

  ngOnDestroy(): void {
    this.stopAnimation();
    if (this.resizeObserver) {
      this.resizeObserver.disconnect();
      this.resizeObserver = null;
    }
    this.tagNodes.forEach((entry) => {
      entry.node.geometry.dispose();
      const material = entry.node.material as THREE.Material;
      material.dispose();
      entry.link.geometry.dispose();
      const linkMaterial = entry.link.material as THREE.Material;
      linkMaterial.dispose();
    });
    this.tagNodes = [];
    this.renderer?.dispose();
    this.scene = null;
    this.camera = null;
    this.renderer = null;
  }

  async reloadData(): Promise<void> {
    this.loadingData = true;
    this.error = null;
    this.notice = null;

    try {
      const [fingerprint, observations, patterns] = await Promise.all([
        firstValueFrom(this.api.getFingerprint()).catch(() => null),
        firstValueFrom(this.api.getObservations({ limit: 150, offset: 0 })).catch(() => null),
        firstValueFrom(this.api.getPatterns({ min_confidence: 0.5 })).catch(() => []),
      ]);

      this.tagPool = this.composeTags(
        fingerprint,
        observations,
        Array.isArray(patterns) ? patterns : []
      );
      this.selectedTagId = this.getRenderableTags()[0]?.id || null;
      this.rebuildTagNodes();
      this.notice = `Loaded ${this.tagPool.length} tags from memory signals.`;
    } catch (err: any) {
      this.error = err?.message || 'Failed to load clone graph data';
    } finally {
      this.loadingData = false;
    }
  }

  private initScene(): void {
    const canvas = this.canvasRef.nativeElement;
    const viewport = this.viewportRef.nativeElement;
    const width = Math.max(320, viewport.clientWidth);
    const height = 640;

    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.Fog(0x06111e, 6, 22);

    this.camera = new THREE.PerspectiveCamera(50, width / height, 0.1, 100);
    this.camera.position.set(0, 1.6, 7.5);

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(width, height, false);

    const hemi = new THREE.HemisphereLight(0x8fd8ff, 0x122339, 1.1);
    this.scene.add(hemi);

    const key = new THREE.DirectionalLight(0xffffff, 0.85);
    key.position.set(3, 6, 7);
    this.scene.add(key);

    const fill = new THREE.DirectionalLight(0x2fb7ff, 0.42);
    fill.position.set(-4, 3, -2);
    this.scene.add(fill);

    this.humanRoot = this.buildHumanModel();
    this.scene.add(this.humanRoot);

    const floor = new THREE.Mesh(
      new THREE.CircleGeometry(4.3, 48),
      new THREE.MeshStandardMaterial({ color: 0x112338, transparent: true, opacity: 0.35 })
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = -2.25;
    this.scene.add(floor);

    this.startAnimation();
    this.rebuildTagNodes();
  }

  private buildHumanModel(): THREE.Group {
    const group = new THREE.Group();

    const skin = new THREE.MeshStandardMaterial({ color: 0xf1c7a2, roughness: 0.65, metalness: 0.08 });
    const suit = new THREE.MeshStandardMaterial({ color: 0x1e364f, roughness: 0.5, metalness: 0.25 });
    const accent = new THREE.MeshStandardMaterial({ color: 0x2ea6ff, roughness: 0.3, metalness: 0.8, emissive: 0x0d3d6a, emissiveIntensity: 0.35 });

    const head = new THREE.Mesh(new THREE.SphereGeometry(0.38, 32, 32), skin);
    head.position.set(0, 1.85, 0);
    group.add(head);

    const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.11, 0.13, 0.16, 16), skin);
    neck.position.set(0, 1.48, 0);
    group.add(neck);

    const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.45, 1.25, 12, 24), suit);
    torso.position.set(0, 0.65, 0);
    group.add(torso);

    const core = new THREE.Mesh(new THREE.SphereGeometry(0.2, 24, 24), accent);
    core.position.set(0, 0.95, 0.46);
    group.add(core);

    const leftArm = new THREE.Mesh(new THREE.CapsuleGeometry(0.12, 0.9, 6, 12), suit);
    leftArm.position.set(-0.7, 0.95, 0);
    leftArm.rotation.z = 0.25;
    group.add(leftArm);

    const rightArm = new THREE.Mesh(new THREE.CapsuleGeometry(0.12, 0.9, 6, 12), suit);
    rightArm.position.set(0.7, 0.95, 0);
    rightArm.rotation.z = -0.25;
    group.add(rightArm);

    const leftLeg = new THREE.Mesh(new THREE.CapsuleGeometry(0.14, 1.0, 6, 14), suit);
    leftLeg.position.set(-0.25, -0.9, 0);
    group.add(leftLeg);

    const rightLeg = new THREE.Mesh(new THREE.CapsuleGeometry(0.14, 1.0, 6, 14), suit);
    rightLeg.position.set(0.25, -0.9, 0);
    group.add(rightLeg);

    const aura = new THREE.Mesh(
      new THREE.TorusGeometry(1.6, 0.03, 20, 80),
      new THREE.MeshBasicMaterial({ color: 0x39beff, transparent: true, opacity: 0.5 })
    );
    aura.rotation.x = Math.PI / 2;
    aura.position.y = -0.2;
    group.add(aura);
    this.ring = aura;

    return group;
  }

  private composeTags(
    fingerprint: FingerprintResponse | null,
    observations: ObservationListResponse | null,
    patterns: PatternItem[]
  ): CloneTag[] {
    const tags: CloneTag[] = [];
    const fp = fingerprint?.fingerprint;
    if (fp) {
      const dm: any = fp.decision_making || {};
      const comm: any = fp.communication || {};
      const priorities: any = fp.priorities || {};
      const riskTolerance = String(dm.risk_tolerance || 'moderate');
      const speedVsThoroughness = Number(dm.speed_vs_thoroughness ?? 0.5);
      const delegationTendency = Number(dm.delegation_tendency ?? 0.5);
      const conflictStyle = String(dm.conflict_resolution_style || 'collaborative');
      const verbosity = String(comm.verbosity || 'moderate');
      const toneUnderPressure = String(comm.tone_under_pressure || 'neutral');
      tags.push(
        { id: 'risk', label: 'Risk tolerance', value: riskTolerance, category: 'behavior', source: 'fingerprint', color: '#18c7bf', priority: 100 },
        { id: 'speed-depth', label: 'Speed vs thoroughness', value: speedVsThoroughness.toFixed(2), category: 'behavior', source: 'fingerprint', color: '#44d4ff', priority: 95 },
        { id: 'delegation', label: 'Delegation tendency', value: delegationTendency.toFixed(2), category: 'behavior', source: 'fingerprint', color: '#26b8b0', priority: 90 },
        { id: 'conflict-style', label: 'Conflict style', value: conflictStyle, category: 'behavior', source: 'fingerprint', color: '#1ed7af', priority: 88 },
        { id: 'verbosity', label: 'Communication verbosity', value: verbosity, category: 'behavior', source: 'fingerprint', color: '#26c7e2', priority: 85 },
        { id: 'tone-pressure', label: 'Tone under pressure', value: toneUnderPressure, category: 'behavior', source: 'fingerprint', color: '#37d2e8', priority: 83 }
      );
      for (const concern of (Array.isArray(priorities.top_recurring_concerns) ? priorities.top_recurring_concerns : []).slice(0, 3)) {
        tags.push({
          id: `concern-${concern}`,
          label: 'Recurring concern',
          value: concern,
          category: 'behavior',
          source: 'fingerprint',
          color: '#2ac3d9',
          priority: 78,
        });
      }
    }

    const situationCounts = observations?.situation_type_counts || {};
    Object.entries(situationCounts)
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .slice(0, 6)
      .forEach(([situation, count], idx) => {
        tags.push({
          id: `situation-${situation}`,
          label: `Decision context`,
          value: `${situation} (${count})`,
          category: 'decision',
          source: 'observation',
          color: idx % 2 === 0 ? '#ff7b6b' : '#ffb169',
          priority: 76 - idx,
        });
      });

    patterns
      .slice()
      .sort((a, b) => Number(b.confidence) - Number(a.confidence))
      .slice(0, 8)
      .forEach((pattern, idx) => {
        tags.push({
          id: `pattern-${pattern.id}`,
          label: `Pattern: ${pattern.pattern_type}`,
          value: `${pattern.statement.slice(0, 54)}${pattern.statement.length > 54 ? '...' : ''}`,
          category: 'decision',
          source: 'pattern',
          color: idx % 2 === 0 ? '#ff8a76' : '#ff9d52',
          priority: 70 - idx,
        });
      });

    return tags.slice(0, 18);
  }

  private rebuildTagNodes(): void {
    if (!this.scene || !this.humanRoot) return;

    for (const entry of this.tagNodes) {
      this.scene.remove(entry.node);
      this.humanRoot.remove(entry.link);
      entry.node.geometry.dispose();
      const mat = entry.node.material as THREE.Material;
      mat.dispose();
      entry.link.geometry.dispose();
      const linkMat = entry.link.material as THREE.Material;
      linkMat.dispose();
    }
    this.tagNodes = [];

    const behaviorRadius = 1.65;
    const decisionRadius = 1.95;
    const activeTags = this.getRenderableTags();
    const behaviorTags = activeTags.filter((item) => item.category === 'behavior');
    const decisionTags = activeTags.filter((item) => item.category === 'decision');
    const behaviorCount = Math.max(1, behaviorTags.length);
    const decisionCount = Math.max(1, decisionTags.length);
    const ordered = activeTags
      .slice()
      .sort((a, b) => b.priority - a.priority)
      .forEach((tag) => {
        const sourceSet = tag.category === 'behavior' ? behaviorTags : decisionTags;
        const idx = sourceSet.findIndex((item) => item.id === tag.id);
        const angle = (Math.max(0, idx) / (tag.category === 'behavior' ? behaviorCount : decisionCount)) * Math.PI * 2;
        const radius = tag.category === 'behavior' ? behaviorRadius : decisionRadius;
        const x = Math.cos(angle) * radius;
        const z = Math.sin(angle) * radius * 0.58;
        const y = tag.category === 'behavior'
          ? 0.05 + Math.sin(angle * 2.1) * 0.85
          : 1.75 + Math.sin(angle * 1.9) * 0.55;

        const node = new THREE.Mesh(
          new THREE.SphereGeometry(tag.category === 'behavior' ? 0.08 : 0.095, 18, 18),
          new THREE.MeshStandardMaterial({
            color: new THREE.Color(tag.color),
            emissive: new THREE.Color(tag.color).multiplyScalar(0.35),
            emissiveIntensity: this.selectedTagId === tag.id ? 1.0 : 0.65,
            roughness: 0.35,
            metalness: 0.5,
          })
        );
        node.position.set(x, y, z);
        this.scene!.add(node);

        const lineGeom = new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(0, tag.category === 'behavior' ? 0.45 : 1.75, 0),
          new THREE.Vector3(x, y, z),
        ]);
        const line = new THREE.Line(
          lineGeom,
          new THREE.LineBasicMaterial({
            color: new THREE.Color(tag.color),
            transparent: true,
            opacity: 0.5,
          })
        );
        this.humanRoot!.add(line);

        this.tagNodes.push({ tag, node, link: line });
      });
  }

  private startAnimation(): void {
    this.stopAnimation();
    const loop = () => {
      if (!this.scene || !this.camera || !this.renderer || !this.humanRoot) return;
      const t = performance.now() * 0.001;
      this.frameCounter += 1;

      this.humanRoot.rotation.y = t * 0.35;
      this.humanRoot.position.y = Math.sin(t * 1.1) * 0.04;
      if (this.ring) {
        this.ring.rotation.z = t * 0.24;
      }

      for (let i = 0; i < this.tagNodes.length; i += 1) {
        const entry = this.tagNodes[i];
        entry.node.position.y += Math.sin(t * 1.8 + i) * 0.0015;
      }

      this.updateZoomTier();
      this.renderer.render(this.scene, this.camera);
      if (this.frameCounter % 2 === 0) {
        this.updateTagProjection();
      }
      this.animationId = requestAnimationFrame(loop);
    };
    this.animationId = requestAnimationFrame(loop);
  }

  private stopAnimation(): void {
    if (this.animationId !== null) {
      cancelAnimationFrame(this.animationId);
      this.animationId = null;
    }
  }

  private updateTagProjection(): void {
    if (!this.camera || !this.renderer) return;
    const width = this.renderer.domElement.clientWidth;
    const height = this.renderer.domElement.clientHeight;
    const buffer: TagProjection[] = [];
    const orderedNodes = this.tagNodes
      .slice()
      .sort((a, b) => b.tag.priority - a.tag.priority);
    const baseMax =
      this.labelMode === 'overview' ? 6 : this.labelMode === 'balanced' ? 12 : 24;
    const zoomAdjustedMax =
      this.zoomTier === 'overview'
        ? Math.min(baseMax, 8)
        : this.zoomTier === 'detail'
          ? baseMax + 4
          : baseMax;
    const acceptedBoxes: Array<{ left: number; top: number; right: number; bottom: number }> = [];
    let visibleCount = 0;
    for (const entry of orderedNodes) {
      const world = new THREE.Vector3();
      entry.node.getWorldPosition(world);
      world.project(this.camera);
      let hidden = world.z < -1 || world.z > 1;
      const left = (world.x * 0.5 + 0.5) * width;
      const top = (-world.y * 0.5 + 0.5) * height;
      if (!hidden) {
        if (visibleCount >= zoomAdjustedMax) {
          hidden = true;
        } else if (this.collides(acceptedBoxes, left, top, 148, 52)) {
          hidden = true;
        } else {
          visibleCount += 1;
          acceptedBoxes.push({
            left: left - 74,
            top: top - 26,
            right: left + 74,
            bottom: top + 26,
          });
        }
      }
      buffer.push({
        id: entry.tag.id,
        left,
        top,
        color: entry.tag.color,
        label: entry.tag.label,
        value: entry.tag.value,
        category: entry.tag.category,
        source: entry.tag.source,
        priority: entry.tag.priority,
        hidden,
        selected: this.selectedTagId === entry.tag.id,
      });
    }
    this.projectedTags = buffer.sort((a, b) => b.priority - a.priority);
  }

  toggleCategory(category: TagCategory): void {
    if (category === 'behavior') {
      this.showBehavior = !this.showBehavior;
    } else {
      this.showDecision = !this.showDecision;
    }
    const activeIds = new Set(this.getRenderableTags().map((tag) => tag.id));
    if (this.selectedTagId && !activeIds.has(this.selectedTagId)) {
      this.selectedTagId = this.getRenderableTags()[0]?.id || null;
    }
    this.rebuildTagNodes();
    this.updateTagProjection();
  }

  toggleSource(source: TagSource): void {
    if (source === 'fingerprint') this.showFingerprint = !this.showFingerprint;
    if (source === 'observation') this.showObservation = !this.showObservation;
    if (source === 'pattern') this.showPattern = !this.showPattern;
    const activeIds = new Set(this.getRenderableTags().map((tag) => tag.id));
    if (this.selectedTagId && !activeIds.has(this.selectedTagId)) {
      this.selectedTagId = this.getRenderableTags()[0]?.id || null;
    }
    this.rebuildTagNodes();
    this.updateTagProjection();
  }

  setLabelMode(mode: 'overview' | 'balanced' | 'full'): void {
    this.labelMode = mode;
    this.updateTagProjection();
  }

  setMinPriority(value: number | string): void {
    const parsed = Math.max(40, Math.min(95, Number(value) || 55));
    this.minPriority = parsed;
    const activeIds = new Set(this.getRenderableTags().map((tag) => tag.id));
    if (this.selectedTagId && !activeIds.has(this.selectedTagId)) {
      this.selectedTagId = this.getRenderableTags()[0]?.id || null;
    }
    this.rebuildTagNodes();
    this.updateTagProjection();
  }

  selectTag(tagId: string): void {
    this.selectedTagId = tagId;
    this.rebuildTagNodes();
    this.updateTagProjection();
  }

  clearSelectedTag(): void {
    this.selectedTagId = null;
    this.rebuildTagNodes();
    this.updateTagProjection();
  }

  explainSelectedTag(tag: CloneTag): string {
    if (tag.category === 'behavior') {
      return 'Behavioral tags represent stable tendencies that shape how work is approached over time. Use these to predict execution style and communication expectations.';
    }
    return 'Decision tags represent pressure points and context-specific choices. Use these to understand why actions changed under uncertainty, risk, or time constraints.';
  }

  private getRenderableTags(): CloneTag[] {
    return this.tagPool.filter((tag) => {
      if (tag.category === 'behavior' && !this.showBehavior) return false;
      if (tag.category === 'decision' && !this.showDecision) return false;
      if (tag.source === 'fingerprint' && !this.showFingerprint) return false;
      if (tag.source === 'observation' && !this.showObservation) return false;
      if (tag.source === 'pattern' && !this.showPattern) return false;
      if (tag.priority < this.minPriority) return false;
      return true;
    });
  }

  private updateZoomTier(): void {
    if (!this.camera) return;
    const distance = this.camera.position.length();
    if (distance >= 8.2) {
      this.zoomTier = 'overview';
    } else if (distance <= 6.0) {
      this.zoomTier = 'detail';
    } else {
      this.zoomTier = 'balanced';
    }
  }

  private collides(
    boxes: Array<{ left: number; top: number; right: number; bottom: number }>,
    left: number,
    top: number,
    width: number,
    height: number
  ): boolean {
    const box = {
      left: left - width / 2,
      right: left + width / 2,
      top: top - height / 2,
      bottom: top + height / 2,
    };
    for (const other of boxes) {
      if (
        box.left < other.right &&
        box.right > other.left &&
        box.top < other.bottom &&
        box.bottom > other.top
      ) {
        return true;
      }
    }
    return false;
  }

  private attachResize(): void {
    const viewport = this.viewportRef.nativeElement;
    this.resizeObserver = new ResizeObserver(() => {
      if (!this.camera || !this.renderer) return;
      const width = Math.max(320, viewport.clientWidth);
      const height = 640;
      this.camera.aspect = width / height;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(width, height, false);
      this.updateTagProjection();
    });
    this.resizeObserver.observe(viewport);
  }
}
