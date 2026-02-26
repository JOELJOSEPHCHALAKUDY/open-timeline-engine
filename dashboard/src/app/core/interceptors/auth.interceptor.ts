import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { AuthService } from '../services/auth.service';

export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService).get();
  const cloned = req.clone({
    setHeaders: {
      Authorization: `Bearer ${auth.token}`,
      'X-TCE-Consumer': auth.consumer,
      'X-TCE-Role': auth.role,
      'X-TCE-Workspace': auth.workspace,
      'X-TCE-User': auth.user,
    },
  });
  return next(cloned);
};
