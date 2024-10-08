<template>
  <v-container>
    <v-layout>
      <v-flex xs12>
        <JobsTable
          header="Jobs"
          :elevation="1"
          :jobs="Object.values(jobs)"
          :loading="loading"
          :performing-bulk-action="performingJobBulkAction"
          :performing-crud-action="performingJobCrudAction"
          @error="handleError"
          @bulk-action="performJobBulkAction"
          @crud-action="performJobCrudAction"
        />
      </v-flex>
    </v-layout>

    <v-snackbar v-model="snackbar" bottom color="error">
      {{ errorMessage }}
      <v-btn flat @click="snackbar = false">Close</v-btn>
    </v-snackbar>
  </v-container>
</template>

<script>
import api from '../../api'
import {
  getJobs,
  executeJob,
  enqueueJob,
  dequeueJob,
  stopJob,
  killJob,
  deleteJob,
  createJob,
  createJobTask
} from '../../api/jobs'
import { getTasks } from '../../api/tasks'
import { getErrorMessage } from '../../utils/errors'
import { Actions as JobCrudActions } from './jobs_overview/JobCrudActions'
import { JobBulkActions } from './jobs_overview/JobBulkActions'
import JobsTable from './jobs_overview/JobsTable'

export default {
  components: {
    JobsTable
  },
  data () {
    return {
      jobs: {},
      usernames: {},
      loading: true,
      performingJobBulkAction: false,
      performingJobCrudAction: [],
      snackbar: false,
      errorMessage: '',
      interval: null
    }
  },
  mounted () {
    this.initView()
    let self = this
    this.interval = setInterval(() => {
      if (self.$route.fullPath !== '/jobs_overview') {
        clearInterval(self.interval)
      } else {
        self.initView()
      }
    }, 10000)
  },
  methods: {
    initView () {
      this.loading = true
      if (this.$store.state.role === 'admin') {
        api
          .request('get', '/users', this.$store.state.accessToken)
          .then(response => {
            response.data.forEach((user) => {
              this.usernames[user.id] = user.username
            })
          })
          .finally(() => {
            this.fetchJobs()
          })
      } else {
        this.fetchJobs()
      }
    },
    fetchJobs () {
      getJobs(this.$store.state.accessToken, this.$store.state.role === 'admin' ? null : this.$store.state.id)
        .then(jobs => {
          jobs.forEach((job) => {
            this.jobs[job.id] = job
            if (this.$store.state.role === 'admin') {
              this.jobs[job.id].username = this.usernames.hasOwnProperty(job.userId) ? this.usernames[job.userId] : undefined
            } else {
              this.jobs[job.id].username = this.$store.state.user
            }
          })
          this.loading = false
        })
        .catch(error => {
          this.loading = false
          this.handleError(error)
        })
    },
    performJobBulkAction (jobs, action) {
      this.performingJobBulkAction = true

      this.performJobsAction(jobs, action)
        .then(() => {
          this.performingJobBulkAction = false
        })
        .catch(error => {
          this.handleError(error)

          this.performingJobBulkAction = false
        })
    },
    performJobCrudAction (job, action) {
      const index = this.performingJobCrudAction.push(job.id) - 1

      this.performJobsAction([job], action)
        .then(() => {
          this.performingJobCrudAction.splice(index, 1)
        })
        .catch(error => {
          this.handleError(error)
          this.performingJobCrudAction.splice(index, 1)
        })
    },
    performJobsAction (jobs, action) {
      let promise = null

      switch (action) {
        case JobCrudActions.Execute:
        case JobBulkActions.Execute:
          promise = this.executeJobs(jobs)
          break
        case JobCrudActions.Enqueue:
          promise = this.enqueueJobs(jobs)
          break
        case JobCrudActions.Dequeue:
          promise = this.dequeueJobs(jobs)
          break
        case JobCrudActions.Stop:
        case JobBulkActions.Stop:
          promise = this.stopJobs(jobs)
          break
        case JobCrudActions.Kill:
        case JobBulkActions.Kill:
          promise = this.killJobs(jobs)
          break
        case JobBulkActions.Copy:
          promise = this.copyJobs(jobs)
          break
        case JobCrudActions.Delete:
        case JobBulkActions.Delete:
          promise = this.deleteJobs(jobs)
          break
        default:
          promise = Promise.reject(new Error(`Unknown action '${action}'`))
      }

      return promise.then(jobs => {
        if (
          action === JobCrudActions.Delete ||
          action === JobBulkActions.Delete
        ) {
          for (const job of jobs) {
            delete this.jobs[job.id]
          }
        } else {
          for (const job of jobs) {
            this.jobs[job.id] = job
          }
        }
      })
    },
    executeJobs (jobs) {
      return Promise.all(
        jobs.map(({ id }) => executeJob(this.$store.state.accessToken, id))
      )
    },
    enqueueJobs (jobs) {
      return Promise.all(
        jobs.map(({ id }) => enqueueJob(this.$store.state.accessToken, id))
      )
    },
    dequeueJobs (jobs) {
      return Promise.all(
        jobs.map(({ id }) => dequeueJob(this.$store.state.accessToken, id))
      )
    },
    stopJobs (jobs) {
      return Promise.all(
        jobs.map(({ id }) => stopJob(this.$store.state.accessToken, id))
      )
    },
    killJobs (jobs) {
      return Promise.all(
        jobs.map(({ id }) => killJob(this.$store.state.accessToken, id))
      )
    },
    copyJobs (jobs) {
      // TODO: This method could be much clearer if written using async/await
      // but can we use async/await?
      const newJobs = jobs.map(job => ({
        name: job.name,
        description: job.description,
        startAt: job.startAt,
        stopAt: job.stopAt,
        userId: this.$store.state.id
      }))
      const createCopies = Promise.all(
        newJobs.map(job => createJob(this.$store.state.accessToken, job))
      )
      const getOriginalTasks = Promise.all(
        jobs.map(({ id }) => getTasks(this.$store.state.accessToken, id))
      )

      return Promise.all([createCopies, getOriginalTasks]).then(
        ([newJobs, originalTasks]) =>
          Promise.all(
            newJobs.map(({ id }, index) => {
              const { command, hostname } = originalTasks[index]

              return createJobTask(this.$store.state.accessToken, id, {
                command,
                hostname,
                jobId: id
              })
            })
          ).then(() => newJobs)
      )
    },
    deleteJobs (jobs) {
      return Promise.all(
        jobs.map(({ id }) => deleteJob(this.$store.state.accessToken, id))
      ).then(() => jobs)
    },
    handleError (error) {
      this.errorMessage = getErrorMessage(error)
      this.snackbar = true
    }
  }
}
</script>
