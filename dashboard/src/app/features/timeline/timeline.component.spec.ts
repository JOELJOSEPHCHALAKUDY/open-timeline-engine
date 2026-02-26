import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import { EventSearchRequest } from '../../core/models';
import { TimelineComponent } from './timeline.component';

describe('TimelineComponent', () => {
  let fixture: ComponentFixture<TimelineComponent>;
  let component: TimelineComponent;
  let apiSpy: jasmine.SpyObj<ApiService>;

  beforeEach(async () => {
    apiSpy = jasmine.createSpyObj<ApiService>('ApiService', ['searchEvents']);
    apiSpy.searchEvents.and.returnValue(
      of({
        events: [],
        total: 0,
        citations: [],
        policy: {},
      })
    );

    await TestBed.configureTestingModule({
      imports: [TimelineComponent],
      providers: [{ provide: ApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(TimelineComponent);
    component = fixture.componentInstance;
  });

  it('uses match_all on initial load when query is empty', () => {
    fixture.detectChanges();
    expect(apiSpy.searchEvents).toHaveBeenCalled();
    const request = apiSpy.searchEvents.calls.mostRecent().args[0] as EventSearchRequest;
    expect(request.match_all).toBeTrue();
    expect(request.query).toBe('*');
  });

  it('uses text query mode when user provides a non-empty query', () => {
    fixture.detectChanges();
    component.query = 'login route';
    component.search();
    const request = apiSpy.searchEvents.calls.mostRecent().args[0] as EventSearchRequest;
    expect(request.match_all).toBeFalse();
    expect(request.query).toBe('login route');
  });
});
