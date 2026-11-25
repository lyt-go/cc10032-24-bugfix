import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
    { path: '/:moduleKey/:entryId(\\d+)', name: 'module-entry', component: WorkbenchView, props: true },
  ],
})

export default router
